import sqlite3
import logging
import re
from pipeline.models import Preferences, paper, newsletter, podcastEpisode, dailyRun, SelectedPaperIDs, StatusEnum, PaperSummary, Newsletter_Content, PapersContext, Podcast_Episode_Content
from pydantic_ai import Agent
import json
from enum import Enum
from pipeline.database import save_to_db, update_row_db
from dotenv import load_dotenv
import requests
import pypdf
import io
from datetime import datetime
import secrets
import uuid
from pprint import pprint
import time
from pipeline.utils import get_papers


# load environment variables
load_dotenv()

# Configure logging output format 
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

WAIT_SECONDS = 5
# utility functions
def extract_pdf_text_from_url(url: str) -> str:
    # fetch PDF content over HTTP
    response = requests.get(url)
    response.raise_for_status()

    # wrap bytes in io.BytesIO and pass to PdfReader
    pdf_file = io.BytesIO(response.content)
    reader = pypdf.PdfReader(pdf_file)

    # Extract text
    return "\n".join([page.extract_text() or "" for page in reader.pages])

def generate_time_id(random_length=4) -> str:
    # Timestamp down to seconds
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    # Append random hex bytes
    random_suffix = secrets.token_hex(random_length // 2)
    return f"{timestamp}_{random_suffix}"

class LLMClient:
    @staticmethod   
    def filter_papers(papers, keywords, papers_per_digest, user_intention):
        # Remove papers that are already in the SQL database
        cursor = None
        conn = None
        try:
            # connect to SQL database file
            conn = sqlite3.connect("app.db")
            # Set row_factory to sqlite3.Row to access columns by name 
            conn.row_factory = sqlite3.Row 
            cursor = conn.cursor()
        except Exception as e:
            logging.error(f"Exception on filter_papers(): Failed to connect to SQL database file: {e}")
        
    
        # create a list to store valid papers that are not yet in the database
        layer1_papers = []
    
        for paper in papers:
            try:
                cursor.execute("SELECT * FROM papers WHERE arxiv_id = ?", (str(paper.arxiv_id),))

                # if there is a result, then paper already exists in database
                single_row = cursor.fetchone()
        
                # otherwise paper does not exist in database yet, so add paper to layer1_papers list
                if not single_row:
                    layer1_papers.append(paper)
            except Exception as e:
                logging.error(f"Exception on filter_papers(): Failed to execute select SQL query for layer1_papers: {e}")

        # close the read connection now so it doesn't hold a lock while save_to_db() writes below
        if conn is not None:
            conn.close()


        # another list of papers that have passed layer 1 and layer 2 (where the paper is relevant to at least one keyword)
        layer2_papers = []       
        # Filter papers to only include papers that are relevant to the keywords
        # Escape words to handle special characters, then join with '|'
        pattern = r"\b(" + "|".join(map(re.escape, keywords)) + r")\b"

        for paper in layer1_papers:
            try:
                # Check for a match
                match = re.search(pattern, paper.abstract, re.IGNORECASE)
            except Exception as e:
                logging.error(f"Exception on filter_papers(): Failed to execute REGEX search pattern for layer2_papers: {e}")

            # only add papers with a keyword match
            if match:
                layer2_papers.append(paper)
   
        layer3_papers = []
        
        # initialize filtering agent
        try:
            agent = Agent(
                'openai:gpt-5-nano', 
                output_type=SelectedPaperIDs,
                system_prompt="You are an expert research assistant and teacher. Analyze the provided list of papers and return only those relevant to the user prompt that will help the user's learning and knowledge growth."
            )
        except Exception as e:
            logging.error(f"Exception on filter_papers(): Initializing filtering agent {e}")
        
        
        if len(layer2_papers) > papers_per_digest:
            # go through all layer2_papers and rank them based on how it helps user_intention and return the top N (papers_per_digest) papers
            user_prompt = (
                f"Select top relevant papers for user intention '{user_intention}' "
                f"and keywords: {keywords}."
            )

            selected_ids = []
            try:
                time.sleep(WAIT_SECONDS)
                # pass context so Pydantic's validator knows the dynamic limit
                result = agent.run_sync(
                    f"User Query: {user_prompt}\n\nCandidate Papers:\n{[p.model_dump() for p in layer2_papers]}",
                    deps={"papers_per_digest": papers_per_digest}
                )
                selected_ids.extend(result.output.selected_ids)
            
                for id in selected_ids:
                    # find the corresponding Paper() object in layer2_papers and add it to layer3_papers list
                    for paper in layer2_papers:
                        if paper.arxiv_id == id:
                            layer3_papers.append(paper)
            except Exception as e:
                logging.error(f"Exception on filter_papers(): Running agent.run_sync() {e}")
                        
        else:
            # otherwise if below or equal to N (papers_per_digest) papers, set those papers to be layer3_papers
            layer3_papers = layer2_papers
            for layer3_paper in layer3_papers:
                selected_ids.append(layer3_paper.arxiv_id)

        # add these passed layer3_papers to the "papers" table in the database
        for paper in layer3_papers:
            try:
                save_to_db(paper, "papers")
                
            except Exception as e:
                logging.error(f"Exception on filter_papers(): Saving layer3_papers to DB: {e}")
        
        
        # create daily_run row and add selected_ids
        dailyrun_id = generate_time_id()
        try:
            daily_run = dailyRun(id = dailyrun_id, started_at=datetime.now(), status=StatusEnum.RUNNING, papers_ids=selected_ids)
            save_to_db(daily_run, "dailyRun")
        except Exception as e:
            logging.error(f"Exception on filter_papers(): Crating daily_run row: {e}")
            
        return dailyrun_id

    @staticmethod
    def summarize_papers(user_intention, tone):
        # Get all paper rows that were fetched today
        papers_list: list[paper] = []
        
        cursor = None
        conn = None
        try:
            # connect to SQL database file
            conn = sqlite3.connect("app.db")
            # Set row_factory to sqlite3.Row to access columns by name 
            conn.row_factory = sqlite3.Row 
            cursor = conn.cursor()
        except Exception as e:
            logging.error(f"Exception on summarize_papers(): Failed to connect to SQL database file: {e}")
        
        
        # get today's date prefix (e.g., '2026-08-24')
        today_date = datetime.now().strftime("%Y-%m-%d")
        try:
            # use LIKE to match any timestamp starting with today's date
            cursor.execute(
                "SELECT * FROM papers WHERE fetched_at LIKE ?", 
                (f"{today_date}%",)
            )
            
            # get all resulting papers that were fetched today
            res = cursor.fetchall()
            
            # add res papers to papers_list
            papers_list.extend(res)
                    
        except Exception as e:
            logging.error(f"Exception on summarize_papers(): Extracting row: {e}")
            
            
        # close the read connection 
        if conn is not None:
            conn.close()
        
        # Initializing summarizer agent
        
        try:
            agent = Agent(
                'openai:gpt-5-nano', 
                output_type=PaperSummary,
                system_prompt="You are an expert research assistant and teacher. Analyze and understand the academic paper so that you can write a comprehensive full summary of the paper and a text on why its relevant for your student's intetion"
            )
        except Exception as e:
            logging.error(f"Exception on filter_papers(): Initializing agent {e}")
                
        
        # Extract pdf from each paper row and summarize
        for paper_row in papers_list:
            try:
                # Must read entire paper first
                pdf_text = extract_pdf_text_from_url(paper_row['pdf_url'])
            
                # Summarize the paper using OpenAI API via Pydantic AI
                # LLM writes short summary per selected paper, saves to ai_summary column and ai_why_relevant column
                result = agent.run_sync(
                    f"Student Intention: {user_intention}\n\n"
                    f"Here is the full text of the paper:\n\n{pdf_text}\n\n"
                    "Understand it thoroughly and write a less than 150 word summary and less than 150 word why_relevant section."
                    f"Must use the following tone: {tone}. Stick to strict word count!"
                )
                 
                summary_data = result.output.ai_summary
                relevant_section = result.output.ai_why_relevant
                
                time.sleep(WAIT_SECONDS)
            except Exception as e:
                logging.error(f"Exception for summarize_papers(): error on running summarizer agent: {e}")
                continue

            try:
                # Write summary_data to ai_summary column
                update_row_db(col_name="ai_summary", new_value=summary_data, id=paper_row['arxiv_id'], table_name="papers")
            
                # Write relevant_section to ai_why_relevant column
                update_row_db(col_name="ai_why_relevant", new_value=relevant_section, id=paper_row['arxiv_id'], table_name="papers")
            except Exception as e:
                logging.error(f"Exception for summarize_papers(): error on updating ai_summary and ai_why_relevant sections: {e}")
            
            time.sleep(WAIT_SECONDS)
        return None
    
    @staticmethod
    def generate_newsletter_content(dailyrun_id, tone, user_intention):
        
        # get papers associated with dailyrun_id
        formatted_papers = get_papers(dailyrun_id)
        deps = PapersContext(
            user_intention=user_intention,
            tone=tone,
            papers=formatted_papers
        )
        
        # initialize newsletter agent
        try:
            agent = Agent(
                'openai:gpt-5-nano', 
                output_type=Newsletter_Content,
                deps_type=PapersContext,
                system_prompt="You are a marketer, writer, and scientific researcher. "
                    "You are writing parts of a newsletter dedicated to helping the user's intention: "
                    f"'{deps.user_intention}'. Generate a captivating title and short intro body "
                    f"summarizing the papers with the following tone: {deps.tone}."
            )
        except Exception as e:
            logging.error(f"Exception on generate_newsletter_content(): Initializing agent {e}")
            
        # Generate newsletter opening body and title summarizing all articles using OpenAI API via Pydantic AI
        title = None
        body = None
        try:
            result = agent.run_sync(
                f"Here are the papers to summarize:\n{json.dumps(formatted_papers, indent=2)}\n\n"
                "Write a clear, captivating intro body under 150 words.",
                deps=deps
            )
        
            title = result.output.title
            body = result.output.body
            
            time.sleep(WAIT_SECONDS)
        except Exception as e:
            logging.error(f"Exception for generate_newsletter_content(): error on running newsletter agent: {e}")
                        
        # generating ID
        now = datetime.now()
        int_id = int(now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond // 1000:03d}")
        # Save to newsletter table
        try:
            newsletter_obj = newsletter(id=int_id, title=title, body_content=body, run_id=dailyrun_id)
            save_to_db(obj=newsletter_obj, table_name="newsletter")
        except Exception as e:
            logging.error(f"Exception for generate_newsletter_content(): adding newsletter row to newsletter table: {e}")
        
        # Update dailyRun row to add newsletter_id
        try:
            update_row_db(col_name="newsletter_id", new_value=int_id, id=dailyrun_id, table_name="dailyRun")
        except Exception as e:
            logging.error(f"Exception for generate_newsletter_content(): updating newsletter_id in dailyRun table: {e}")
        
        
        return None
    
    @staticmethod
    def generate_podcast_script(dailyrun_id, tone, user_intention):
        
        # Generate podcast script using OpenAI API via Pydantic AI
        # LLM writes podcast script based on the summaries of the papers, saves to podcast_script column.
         
        # get papers associated with dailyrun_id
        formatted_papers = get_papers(dailyrun_id)
        deps = PapersContext(
            user_intention=user_intention,
            tone=tone,
            papers=formatted_papers
        )
                
        # initialize newsletter agent
        try:
            agent = Agent(
                'openai:gpt-5-nano', 
                output_type=Podcast_Episode_Content,
                deps_type=PapersContext,
                system_prompt="You are a podcaster, writer, and scientific researcher for the show Research Digest. "
                    "You are writing an engaging podcast script body and title dedicated to helping the user's intention: "
                    f"'{deps.user_intention}'. Generate a podcast script body and title where you are the only speaker"
                    f"summarizing the papers with the following tone: {deps.tone}."
                    f"Also generate a short description of the podcast episode"
                    "STRICTLY FOLLOW: When creating the script, do not add any labels, sections, or headings. Just add the text you would say. "
            )
        except Exception as e:
            logging.error(f"Exception on generate_podcast_script(): Initializing agent {e}")
                    
        # Generate title and script via Pydantic AI and Open AI API
        title = None
        try:
            result = agent.run_sync(
                        f"Here are the papers to summarize:\n{json.dumps(formatted_papers, indent=2)}\n\n"
                        "Write a clear, captivating podcast script under 800 words, title, and a short description (under 100 words) of the podcast episode",
                        deps=deps
                    )
                
            title = result.output.podcast_title
            body = result.output.script_body
            description = result.output.description
            time.sleep(WAIT_SECONDS)
        except Exception as e:
            logging.error(f"Exception for generate_podcast_script(): error on running podcast writer agent: {e}")
                                
        # generating ID
        now = datetime.now()
        int_id = int(now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond // 1000:03d}")
        
        # Save to podcastEpisode table
        try:
            podcast_episode_obj = podcastEpisode(id=int_id, title=title, description=description, script=body, run_id=dailyrun_id)
            save_to_db(obj=podcast_episode_obj, table_name="podcastEpisode")
        except Exception as e:
            logging.error(f"Exception for generate_podcast_script(): adding podcast row to podcastEpisode table: {e}")
                
        # Update dailyRun row to add podcast_id
        try:
            update_row_db(col_name="podcast_id", new_value=int_id, id=dailyrun_id, table_name="dailyRun")
        except Exception as e:
            logging.error(f"Exception for generate_podcast_script(): updating podcast_id in dailyRun table: {e}")
                
                
        return None