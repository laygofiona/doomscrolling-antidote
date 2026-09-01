# build an image with Python 3.12 Alpine
FROM python:3.12-alpine

# install supercronic & tzdata (for timezone support)
RUN apk add --no-cache supercronic tzdata

# set local timezone (Adjust to your local timezone, e.g., America/Toronto, America/New_York)
ENV TZ=America/Toronto

# set working directory
WORKDIR /code

# copy dependencies and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# copy source code along with ./crontab file
COPY . .

EXPOSE 8000

# Run supercronic pointing to the created crontab
CMD ["supercronic", "/code/crontab"]