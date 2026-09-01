"""End-to-end daily pipeline: fetch papers, summarize, and publish newsletter/podcast."""

import asyncio
import datetime
import json
import logging
import mimetypes
import os
import re
import shutil
import smtplib
import sqlite3
import tempfile
import time
import uuid
from email.message import EmailMessage

import arxiv
import boto3
import edge_tts
import requests
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import ClientError
from feedgen.feed import FeedGenerator
from jinja2 import Environment, FileSystemLoader, select_autoescape
from mutagen.id3 import ID3, TALB, TIT2, TPE1, error as ID3Error
from mutagen.mp3 import MP3

from llm.agents import LLMClient
from pipeline.database import get_connection, init_db, update_row_db
from pipeline.logging_config import configure_logging
from pipeline.models import Paper, Preferences, StatusEnum
from pipeline.utils import get_id, get_papers

WAIT_TIME = 5  # seconds

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465  # Use 465 for SSL, or 587 for TLS/STARTTLS
BUCKET_NAME = os.getenv("BUCKET_NAME")
REGION = "ca-central-1"
COVER_IMAGE_PATH = "media/cover_image.jpeg"

# RSS feed / iTunes metadata (no dedicated "podcast" table, so these are static)
PODCAST_TITLE = "Research Digest"
PODCAST_DESCRIPTION = (
    "A daily, AI-narrated digest of the latest arXiv papers picked for you."
)
PODCAST_AUTHOR = "Research Digest"
PODCAST_OWNER_EMAIL = os.getenv("SENDER_EMAIL", "")
PODCAST_LANGUAGE = "en-us"
PODCAST_CATEGORY = "Technology"
PODCAST_EXPLICIT = "no"
RSS_S3_KEY = "rss/feed.xml"
COVER_IMAGE_S3_KEY = (
    "podcasts/cover.jpg"  # key ends in .jpg/.png: required by itunes:image
)
PODCAST_WEBSITE_S3_KEY = "podcast/index.html"
RSS_FEED_URL = f"https://{BUCKET_NAME}.s3.{REGION}.amazonaws.com/{RSS_S3_KEY}"
PODCAST_WEBSITE_URL = (
    f"https://{BUCKET_NAME}.s3.{REGION}.amazonaws.com/{PODCAST_WEBSITE_S3_KEY}"
)

# Podcast Namespace (Podcasting 2.0) spec: https://github.com/Podcastindex-org/podcast-namespace
PODCAST_NAMESPACE_XMLNS = "https://podcastindex.org/namespace/1.0"
PODCAST_NAMESPACE_GUID_SEED = uuid.UUID("ead4c236-bf58-58c6-a2c6-a6b28d128cb6")

configure_logging()

REQUEST_TIMEOUT = 10  # seconds

# Utility functions

# Primary functions for the pipeline


def read_config_json(path: str):
    """Load pipeline preferences from a config JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        config_data = json.load(f)
    preferences = Preferences(**config_data)
    return preferences


async def generate_audio(text, voice, rate, output_file):
    """Synthesize speech audio for text with edge-tts and save it to output_file."""
    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate)
    await communicate.save(output_file)


def _tag_podcast_audio(mp3_path, episode_title):
    # edge-tts writes bare MP3 frames with no ID3 tags, which podcast validators require
    try:
        audio = MP3(mp3_path, ID3=ID3)
        try:
            audio.add_tags()
        except ID3Error:
            pass  # tags already present
        audio.tags.add(TIT2(encoding=3, text=episode_title))
        audio.tags.add(TPE1(encoding=3, text=PODCAST_AUTHOR))
        audio.tags.add(TALB(encoding=3, text=PODCAST_TITLE))
        # ID3v2.3 (not mutagen's v2.4 default) + a v1 tag for widest validator/player compatibility
        audio.save(v1=2, v2_version=3)
    except (OSError, ID3Error) as e:
        logging.error("Exception on _tag_podcast_audio(): %s", e)


def fetch_papers(categories, keywords, limit):
    """Fetch the latest papers from arXiv for the given categories and keywords."""
    # use arXiv API to get the most popular papers
    # fetch papers at limit
    if not categories and not keywords:
        raise ValueError("At least one category or keyword must be specified.")

    # get the latest max 5 papers from each category
    papers_arr = []
    for category in categories:
        try:
            search = arxiv.Search(
                query=f"cat:{category}",
                max_results=limit,
                sort_by=arxiv.SortCriterion.SubmittedDate,
            )

            for res in arxiv.Client().results(search):
                # create paper object, use local time for datetime
                paper_to_add = Paper(
                    arxiv_id=res.get_short_id(),
                    title=res.title,
                    authors=[author.name for author in res.authors],
                    abstract=res.summary,
                    categories=res.categories,
                    pdf_url=res.pdf_url,
                    arxiv_url=res.entry_id,
                    updated_at=res.updated.strftime("%Y-%m-%d %H:%M:%S"),
                    fetched_at=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    primary_category=category,
                )
                papers_arr.append(paper_to_add)

            time.sleep(WAIT_TIME)  # Wait to avoid hitting the API rate limit
        except Exception as e:  # pylint: disable=broad-exception-caught
            # arxiv's client can fail in library-specific ways (HTTP, parsing, rate
            # limiting); log and continue with the next category rather than abort.
            logging.error("Exception on fetch_papers(): %s", e)
    return papers_arr


def generate_newsletter_html(dailyrun_id):
    """Render the newsletter HTML for a given dailyrun_id."""
    # Get the papers associated with dailyrun_id
    papers_processed = get_papers(dailyrun_id=dailyrun_id)

    # Decode the JSON-string authors/categories columns into lists for the template
    for p in papers_processed:
        try:
            p["authors"] = (
                json.loads(p["authors"])
                if isinstance(p.get("authors"), str)
                else p.get("authors", [])
            )
        except (TypeError, json.JSONDecodeError) as e:
            logging.error(
                "Exception on generate_newsletter_html(): Decoding authors for paper %s: %s",
                p.get("arxiv_id"),
                e,
            )
            p["authors"] = []
        try:
            p["categories"] = (
                json.loads(p["categories"])
                if isinstance(p.get("categories"), str)
                else p.get("categories", [])
            )
        except (TypeError, json.JSONDecodeError) as e:
            logging.error(
                "Exception on generate_newsletter_html(): Decoding categories for paper %s: %s",
                p.get("arxiv_id"),
                e,
            )
            p["categories"] = []

    # connect to SQL database file
    cursor = None
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
    except sqlite3.Error as e:
        logging.error(
            "Exception on generate_newsletter_html(): Failed to connect to SQL database file: %s",
            e,
        )
        return None

    title = ""
    intro = ""
    try:
        # Get the newsletter id associated with dailyrun_id
        newsletter_id = None
        cursor.execute(
            "SELECT newsletter_id FROM dailyRun WHERE id = ?", (dailyrun_id,)
        )
        res = cursor.fetchone()
        if res:
            newsletter_id = res["newsletter_id"]

        # Get the newsletter associated with dailyrun_id
        if newsletter_id is not None:
            cursor.execute(
                "SELECT title, body_content FROM newsletter WHERE id = ?",
                (newsletter_id,),
            )
            res = cursor.fetchone()
            if res:
                title = res["title"]
                intro = res["body_content"]
    except sqlite3.Error as e:
        logging.error(
            "Exception on generate_newsletter_html(): Extracting title, body_content "
            "from newsletter %s",
            e,
        )
    finally:
        conn.close()

    # Jinja2 template renders HTML from paper summaries
    env = Environment(
        loader=FileSystemLoader(TEMPLATES_DIR),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("newsletter.html")

    # returns email html
    return template.render(title=title, intro=intro, papers=papers_processed)


def email_newsletter(newsletter_html, sender_email, receiver_email, newsletter_id):
    """Send the newsletter HTML via SMTP and record the send time."""
    # construct email message and details
    now = datetime.datetime.now()
    msg = EmailMessage()
    msg["Subject"] = f'Research Digest - Issue {now.strftime("%A, %B %d, %Y")}'
    msg["From"] = sender_email
    msg["To"] = receiver_email

    # Optional plain-text fallback for email clients that don't support HTML
    msg.set_content(
        "Here is your daily update! (Please enable HTML to view this email properly)"
    )

    msg.add_alternative(newsletter_html, subtype="html")

    # establish a secure connection and send the email
    try:
        with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
            server.login(sender_email, os.getenv("APP_PASSWORD"))
            server.send_message(msg)
        logging.info("Email sent successfully!")

        # update newsletter row table
        update_row_db(
            col_name="sent_at",
            new_value=now,
            row_id=newsletter_id,
            table_name="newsletter",
        )

    except (smtplib.SMTPException, OSError, sqlite3.Error) as e:
        logging.error("Exception on email_newsletter(): Failed to send email. Error: %s", e)


def build_podcast_episode(podcast_id):
    """Generate the podcast episode MP3 for podcast_id and return its local path."""
    # Retrieve podcast row from podcastEpisode table
    conn = None
    podcast_row = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM podcastEpisode WHERE id = ?", (podcast_id,))
        podcast_row = cursor.fetchone()
    except sqlite3.Error as e:
        logging.error(
            "Exception on build_podcast_episode(): Failed to fetch podcastEpisode row: %s", e
        )
        return None
    finally:
        if conn is not None:
            conn.close()

    if podcast_row is None:
        logging.error(
            "Exception on build_podcast_episode(): No podcastEpisode row found for id %s",
            podcast_id,
        )
        return None

    # create a temp folder
    temp_path = os.path.join(tempfile.gettempdir(), "research_digest_podcast")

    if not os.path.isdir(temp_path):
        # create folder
        os.mkdir(temp_path)
    else:
        # remove contents inside
        for filename in os.listdir(temp_path):
            path = os.path.join(temp_path, filename)
            try:
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(path)
            except OSError as e:
                logging.error(
                    "Exception on build_podcast_episode() on removing contents inside %s: %s",
                    temp_path,
                    e,
                )

    # edge-tts converts script to MP3
    output_path = f"{temp_path}/{podcast_id}.mp3"
    # creates podcast episode mp3 file in a temporary folder locally
    try:
        asyncio.run(
            generate_audio(
                text=podcast_row["script"],
                voice="en-US-BrianMultilingualNeural",
                rate="+0%",
                output_file=output_path,
            )
        )
    except Exception as e:  # pylint: disable=broad-exception-caught
        # edge-tts calls an external TTS service; treat any failure as a skip-and-log.
        logging.error("Exception on build_podcast_episode(): Failed to generate audio: %s", e)
        return None

    # write ID3 tags (title/artist/album) so podcast feed validators can read episode metadata
    _tag_podcast_audio(output_path, podcast_row["title"])

    # update values for duration_seconds, file_size_bytes for podcastEpisode row in table
    try:
        file_size_bytes = os.path.getsize(output_path)
        update_row_db(
            col_name="file_size_bytes",
            new_value=file_size_bytes,
            row_id=podcast_id,
            table_name="podcastEpisode",
        )

        duration_seconds = int(MP3(output_path).info.length)
        update_row_db(
            col_name="duration_seconds",
            new_value=duration_seconds,
            row_id=podcast_id,
            table_name="podcastEpisode",
        )
    except (OSError, sqlite3.Error) as e:
        logging.error(
            "Exception on build_podcast_episode(): Failed to update duration_seconds/"
            "file_size_bytes: %s",
            e,
        )

    return output_path


def _ensure_public_bucket(s3):
    bucket_exists = False

    # Check if the bucket exists
    try:
        s3.head_bucket(Bucket=BUCKET_NAME)
        bucket_exists = True
    except ClientError as e:
        error_code = e.response["Error"]["Code"]
        # 404 means the bucket does not exist and needs to be created
        if error_code != "404":
            # If it's a 403 or other permission issue, re-raise or log it
            logging.warning("head_bucket returned code %s: %s", error_code, e)

    # Create the bucket ONLY if it doesn't exist
    if not bucket_exists:
        try:
            if REGION == "us-east-1":
                s3.create_bucket(Bucket=BUCKET_NAME)
            else:
                s3.create_bucket(
                    Bucket=BUCKET_NAME,
                    CreateBucketConfiguration={"LocationConstraint": REGION},
                )
        except ClientError as e:
            error_code = e.response["Error"]["Code"]
            if error_code in ["BucketAlreadyOwnedByYou", "BucketAlreadyExists"]:
                logging.info("Bucket %s already exists.", BUCKET_NAME)
                bucket_exists = True
            else:
                raise

    # Best-effort: (re-)apply public access settings even on an existing
    # bucket, since a prior run may have failed to apply them. A permission
    # error here shouldn't block uploads to a bucket that's already public.
    try:
        s3.put_public_access_block(
            Bucket=BUCKET_NAME,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": False,
                "IgnorePublicAcls": False,
                "BlockPublicPolicy": False,
                "RestrictPublicBuckets": False,
            },
        )
    except ClientError as e:
        logging.warning("put_public_access_block failed for %s: %s", BUCKET_NAME, e)

    # Apply Public Read Policy
    public_read_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "PublicReadGetObject",
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{BUCKET_NAME}/*",
            }
        ],
    }
    try:
        s3.put_bucket_policy(Bucket=BUCKET_NAME, Policy=json.dumps(public_read_policy))
    except ClientError as e:
        logging.warning("put_bucket_policy failed for %s: %s", BUCKET_NAME, e)


def upload_podcast_episode(podcast_ep_path):
    """Upload a podcast episode file to S3 and return its public URL."""
    s3 = boto3.client("s3", region_name=REGION)

    try:
        _ensure_public_bucket(s3)
    except ClientError as e:
        logging.error("Exception on upload_podcast_episode(): Failed to create bucket: %s", e)
        return None

    # Upload the file under a stable key
    s3_key = f"podcasts/{os.path.basename(podcast_ep_path)}"
    try:
        s3.upload_file(
            podcast_ep_path,
            BUCKET_NAME,
            s3_key,
            ExtraArgs={"ContentType": "audio/mpeg"},
        )
    except (ClientError, S3UploadFailedError, OSError) as e:
        logging.error("Exception on upload_podcast_episode(): Failed to upload file: %s", e)
        return None

    # build URL
    url = f"https://{BUCKET_NAME}.s3.{REGION}.amazonaws.com/{s3_key}"

    # check if url request is 200
    try:
        res = requests.head(url, timeout=REQUEST_TIMEOUT)
        if res.status_code != 200:
            logging.error(
                "Exception on upload_podcast_episode(): URL check failed with status %s: %s",
                res.status_code,
                url,
            )
    except requests.exceptions.RequestException as e:
        logging.error("Exception on upload_podcast_episode(): Failed to verify URL: %s", e)

    # return url
    return url


def _upload_podcast_website(s3, cover_image_url):
    # Podcast feed validators check that the channel's <link> resolves to an actual
    # webpage; without this, that <link> would have to point at the feed XML itself.
    cover_img_html = (
        f'<img src="{cover_image_url}" alt="{PODCAST_TITLE} cover art" width="300">'
        if cover_image_url
        else ""
    )
    website_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{PODCAST_TITLE}</title>
<meta name="description" content="{PODCAST_DESCRIPTION}">
</head>
<body>
<h1>{PODCAST_TITLE}</h1>
<p>{PODCAST_DESCRIPTION}</p>
{cover_img_html}
<p><a href="{RSS_FEED_URL}">Subscribe via RSS</a></p>
</body>
</html>"""
    try:
        s3.put_object(
            Bucket=BUCKET_NAME,
            Key=PODCAST_WEBSITE_S3_KEY,
            Body=website_html.encode("utf-8"),
            ContentType="text/html",
        )
    except ClientError as e:
        logging.error(
            "Exception on _upload_podcast_website(): Failed to upload website page: %s", e
        )


def _add_podcast_namespace(feed_path, feed_url):
    # feedgen's "podcast" extension only implements the itunes namespace/tags; it has
    # no support for the separate Podcasting 2.0 "podcast" namespace, so patch it in.
    with open(feed_path, "r", encoding="utf-8") as f:
        xml_content = f.read()

    if "xmlns:podcast=" not in xml_content:
        xml_content = xml_content.replace(
            "<rss ",
            f'<rss xmlns:podcast="{PODCAST_NAMESPACE_XMLNS}" ',
            1,
        )

    if "<podcast:guid>" not in xml_content:
        protocol_less_url = re.sub(r"^[a-zA-Z]+://", "", feed_url).rstrip("/")
        podcast_guid = str(uuid.uuid5(PODCAST_NAMESPACE_GUID_SEED, protocol_less_url))
        xml_content = xml_content.replace(
            "<channel>",
            f"<channel>\n    <podcast:guid>{podcast_guid}</podcast:guid>",
            1,
        )

    with open(feed_path, "w", encoding="utf-8") as f:
        f.write(xml_content)


def _upload_cover_image(s3):
    try:
        content_type, _ = mimetypes.guess_type(COVER_IMAGE_PATH)
        s3.upload_file(
            COVER_IMAGE_PATH,
            BUCKET_NAME,
            COVER_IMAGE_S3_KEY,
            ExtraArgs={"ContentType": content_type or "image/jpeg"},
        )
        return f"https://{BUCKET_NAME}.s3.{REGION}.amazonaws.com/{COVER_IMAGE_S3_KEY}"
    except (ClientError, S3UploadFailedError, OSError) as e:
        logging.error("Exception on regenerate_rss_feed(): Failed to upload cover image: %s", e)
        return None


def _get_published_episodes():
    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM podcastEpisode WHERE s3_url IS NOT NULL "
            "AND published_at IS NOT NULL ORDER BY published_at DESC"
        )
        return cursor.fetchall()
    except sqlite3.Error as e:
        logging.error(
            "Exception on regenerate_rss_feed(): Failed to read podcastEpisode rows: %s", e
        )
        return None
    finally:
        if conn is not None:
            conn.close()


def _build_feed_generator(cover_image_url):
    fg = FeedGenerator()
    fg.load_extension("podcast")
    fg.title(PODCAST_TITLE)
    fg.link(href=PODCAST_WEBSITE_URL, rel="alternate")
    fg.link(href=RSS_FEED_URL, rel="self", type="application/rss+xml")
    fg.description(PODCAST_DESCRIPTION)
    fg.language(PODCAST_LANGUAGE)
    fg.generator("python-feedgen")
    fg.lastBuildDate(datetime.datetime.now(datetime.timezone.utc))
    fg.podcast.itunes_author(PODCAST_AUTHOR)
    if PODCAST_OWNER_EMAIL:
        fg.podcast.itunes_owner(name=PODCAST_AUTHOR, email=PODCAST_OWNER_EMAIL)
    fg.podcast.itunes_category(PODCAST_CATEGORY)
    fg.podcast.itunes_explicit(PODCAST_EXPLICIT)
    fg.podcast.itunes_type("episodic")
    if cover_image_url:
        fg.podcast.itunes_image(cover_image_url)
        fg.image(url=cover_image_url, title=PODCAST_TITLE, link=PODCAST_WEBSITE_URL)
    return fg


def _add_episode_entries(fg, episodes, cover_image_url):
    for ep in episodes:
        fe = fg.add_entry()
        fe.id(ep["s3_url"])
        fe.title(ep["title"])
        fe.description(ep["description"])
        fe.link(href=ep["s3_url"])
        fe.enclosure(ep["s3_url"], str(ep["file_size_bytes"] or 0), "audio/mpeg")

        published_dt = datetime.datetime.fromisoformat(ep["published_at"])
        if published_dt.tzinfo is None:
            published_dt = published_dt.replace(tzinfo=datetime.timezone.utc)
        fe.pubDate(published_dt)

        if ep["duration_seconds"]:
            fe.podcast.itunes_duration(ep["duration_seconds"])
        fe.podcast.itunes_explicit(PODCAST_EXPLICIT)
        if cover_image_url:
            fe.podcast.itunes_image(cover_image_url)


def regenerate_rss_feed():
    """Rebuild the RSS feed XML from every published podcast episode in the DB."""
    s3 = boto3.client("s3", region_name=REGION)
    try:
        _ensure_public_bucket(s3)
    except ClientError as e:
        logging.error("Exception on regenerate_rss_feed(): Failed to create bucket: %s", e)
        return None

    # adds cover image to bucket
    cover_image_url = _upload_cover_image(s3)

    # channel <link> must resolve to a real webpage, not the feed XML itself
    _upload_podcast_website(s3, cover_image_url)

    # reads DB, produces XML
    episodes = _get_published_episodes()
    if episodes is None:
        return None

    # using feedgen, with all required iTunes tags
    fg = _build_feed_generator(cover_image_url)
    _add_episode_entries(fg, episodes, cover_image_url)

    # save to a .xml file in /temp folder
    temp_path = os.path.join(tempfile.gettempdir(), "research_digest_podcast")
    if not os.path.isdir(temp_path):
        os.mkdir(temp_path)
    feed_path = os.path.join(temp_path, "feed.xml")
    try:
        fg.rss_file(feed_path, pretty=True)
    except OSError as e:
        logging.error("Exception on regenerate_rss_feed(): Failed to write RSS XML file: %s", e)
        return None

    # feedgen has no support for the Podcasting 2.0 "podcast" namespace, so patch it in
    _add_podcast_namespace(feed_path, RSS_FEED_URL)

    return feed_path


def upload_rss_to_s3(feed_path):
    """Upload the RSS feed file to S3 and return its public URL."""
    s3 = boto3.client("s3", region_name=REGION)
    try:
        _ensure_public_bucket(s3)
    except ClientError as e:
        logging.error("Exception on upload_rss_to_s3(): Failed to create bucket: %s", e)
        return None

    # uploads that XML to S3 at a stable key with content-type application/rss+xml
    # (public read comes from the bucket policy)
    try:
        s3.upload_file(
            feed_path,
            BUCKET_NAME,
            RSS_S3_KEY,
            ExtraArgs={"ContentType": "application/rss+xml"},
        )
    except (ClientError, S3UploadFailedError, OSError) as e:
        logging.error("Exception on upload_rss_to_s3(): Failed to upload RSS feed: %s", e)
        return None

    # check if url request is 200
    try:
        res = requests.head(RSS_FEED_URL, timeout=REQUEST_TIMEOUT)
        if res.status_code != 200:
            logging.error(
                "Exception on upload_rss_to_s3(): URL check failed with status %s: %s",
                res.status_code,
                RSS_FEED_URL,
            )
    except requests.exceptions.RequestException as e:
        logging.error("Exception on upload_rss_to_s3(): Failed to verify URL: %s", e)

    return RSS_FEED_URL


def main():
    """Run the full daily pipeline: fetch, filter, summarize, and publish."""
    # initialize database
    init_db()
    preferences = read_config_json(
        "config.json"
    )

    logging.info("Executing fetch_papers()...")
    # Get hot arXiv papers + their details from today, per config.json's categories/keywords
    papers: list[Paper] = fetch_papers(
        preferences.arxiv_categories,
        preferences.keywords,
        preferences.max_papers_fetched_per_category,
    )
    logging.info("Papers fetched! These are the papers fetched:")

    # Filter papers to get top N (papers_per_digest) ready, store in database table papers
    logging.info("Filtering papers...")
    dailyrun_id = LLMClient.filter_papers(
        papers,
        preferences.keywords,
        preferences.papers_per_digest,
        preferences.user_intention,
    )
    logging.info(
        "Papers filtered! Only %s were selected with user intention of %s",
        preferences.papers_per_digest,
        preferences.user_intention,
    )

    logging.info(
        "Summarizing papers to populate ai_summary and ai_why_relevant fields ..."
    )
    LLMClient.summarize_papers(preferences.user_intention, preferences.tone)
    logging.info("Generating newsletter content...")
    LLMClient.generate_newsletter_content(
        dailyrun_id, preferences.tone, preferences.user_intention
    )
    logging.info("Generating podcast script...")
    LLMClient.generate_podcast_script(
        dailyrun_id, preferences.tone, preferences.user_intention
    )

    logging.info("Sending newsletter...")
    # use existing newsletter tempalte for html, deterministc
    newsletter_html = generate_newsletter_html(dailyrun_id)
    newsletter_id = get_id(id_type="newsletter", dailyrun_id=dailyrun_id)
    email_newsletter(
        newsletter_html,
        os.getenv("SENDER_EMAIL"),
        os.getenv("RECEIVER_EMAIL"),
        newsletter_id,
    )
    logging.info("Newsletter sent to %s...", os.getenv("SENDER_EMAIL"))
    # gets returned s3 url and metadata
    podcast_id = get_id(id_type="podcastEpisode", dailyrun_id=dailyrun_id)
    logging.info("Building today's podcast episode...")
    podcast_ep_path = build_podcast_episode(podcast_id)
    # upload podcast episode to RSS
    # boto3 pushes MP3 to S3, saves URL + metadata to podcast_episodes
    if podcast_ep_path:
        logging.info("Uploading today's podcast episode to S3...")
        podcast_url = upload_podcast_episode(podcast_ep_path=podcast_ep_path)
        if podcast_url:
            update_row_db(
                col_name="s3_url",
                new_value=podcast_url,
                row_id=podcast_id,
                table_name="podcastEpisode",
            )
            update_row_db(
                col_name="published_at",
                new_value=datetime.datetime.now().isoformat(),
                row_id=podcast_id,
                table_name="podcastEpisode",
            )

            # regenerate the RSS feed so podcast platforms pick up the new episode
            logging.info("Generating RSS Feed file...")
            feed_path = regenerate_rss_feed()
            if feed_path:
                logging.info("Uploading RSS Feed file to S3...")
                rss_feed_url = upload_rss_to_s3(feed_path)

                # update dailyRun row completed_at and status columns
                if rss_feed_url:
                    logging.info("Generated RSS Feed URL: %s", rss_feed_url)
                    update_row_db(
                        col_name="completed_at",
                        new_value=datetime.datetime.now(),
                        row_id=dailyrun_id,
                        table_name="dailyRun",
                    )
                    update_row_db(
                        col_name="status",
                        new_value=StatusEnum.SUCCESS,
                        row_id=dailyrun_id,
                        table_name="dailyRun",
                    )
                else:
                    logging.warning("RSS Feed URL not generated.")
                    update_row_db(
                        col_name="status",
                        new_value=StatusEnum.FAILED,
                        row_id=dailyrun_id,
                        table_name="dailyRun",
                    )


if __name__ == "__main__":
    main()
