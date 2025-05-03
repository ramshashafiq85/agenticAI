import imaplib
import smtplib
import email
from email.header import decode_header
from email.mime.text import MIMEText
import logging
import os
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_fixed
from agents import Agent,AsyncOpenAI,OpenAIChatCompletionsModel,Runner
from agents.run import RunConfig
import openai
import os
print(os.getenv("OPENAI_API_KEY"))



# Set up logging
logging.basicConfig(filename="email_agent.log", level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# Load environment variables
load_dotenv()
EMAIL_USER = os.getenv("EMAIL_USER")
EMAIL_PASS = os.getenv("EMAIL_PASS")
gemini_api_key = os.getenv("GEMINI_API_KEY")

if not all([EMAIL_USER, EMAIL_PASS, gemini_api_key]):
    logging.error("Missing environment variables. Please check .env file.")
    raise ValueError("Missing environment variables.")

# Initialize Agents
classifier_agent = Agent(
    name="EmailClassifier",
    instructions="""...""",  # Keep instructions as-is
    model="gpt-3.5-turbo"
)

reply_agent = Agent(
    name="EmailReply",
    instructions="""...""",
    model="gpt-3.5-turbo"
)

action_agent = Agent(
    name="EmailAction",
    instructions="""...""",
    model="gpt-3.5-turbo"
)

def connect_to_inbox(email_user, email_pass, imap_server="imap.gmail.com"):
    try:
        mail = imaplib.IMAP4_SSL(imap_server)
        mail.login(email_user, email_pass)
        mail.select("INBOX")
        logging.info("Connected to inbox.")
        return mail
    except Exception as e:
        logging.error(f"Inbox connection failed: {e}")
        raise

def fetch_emails(mail, num_emails=5):
    try:
        status, messages = mail.search(None, "ALL")
        email_ids = messages[0].split()[-num_emails:]
        emails = []

        for email_id in email_ids:
            status, msg_data = mail.fetch(email_id, "(RFC822)")
            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            subject, encoding = decode_header(msg["subject"] or "")[0]
            if isinstance(subject, bytes):
                subject = subject.decode(encoding or "utf-8", errors="ignore")

            from_ = msg.get("from")

            # Safe email body extraction
            body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    content_type = part.get_content_type()
                    if content_type == "text/plain" and not part.get("Content-Disposition"):
                        body_bytes = part.get_payload(decode=True)
                        if body_bytes:
                            body = body_bytes.decode(errors="ignore")
                            break
            else:
                body_bytes = msg.get_payload(decode=True)
                if body_bytes:
                    body = body_bytes.decode(errors="ignore")

            emails.append({"id": email_id, "subject": subject or "", "from": from_ or "", "body": body or ""})

        logging.info(f"Fetched {len(emails)} emails.")
        return emails
    except Exception as e:
        logging.error(f"Failed to fetch emails: {e}")
        raise

@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
def classify_email(email):
    try:
        input_text = f"Subject: {email['subject']}\nFrom: {email['from']}\nBody: {email['body']}"
        logging.info(f"Classifying email {email['id']}: {input_text}")
        result = Runner.run_sync(classifier_agent, input_text)
        category = result.final_output.strip()
        logging.info(f"Email {email['id']} classified as: {category}")
        return category
    except Exception as e:
        logging.error(f"Classification failed for email {email['id']}: {e}")
        raise

@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
def generate_reply(email):
    try:
        input_text = f"Subject: {email['subject']}\nFrom: {email['from']}\nBody: {email['body']}"
        logging.info(f"Generating reply for email {email['id']}: {input_text}")
        result = Runner.run_sync(reply_agent, input_text)
        reply = result.final_output.strip()
        logging.info(f"Generated reply: {reply}")
        return reply
    except Exception as e:
        logging.error(f"Reply generation failed for email {email['id']}: {e}")
        raise

def send_reply(email_user, email_pass, recipient, subject, body, smtp_server="smtp.gmail.com", smtp_port=587):
    try:
        msg = MIMEText(body)
        msg["Subject"] = f"Re: {subject}"
        msg["From"] = email_user
        msg["To"] = recipient

        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(email_user, email_pass)
            server.send_message(msg)

        logging.info(f"Sent reply to {recipient}")
    except Exception as e:
        logging.error(f"Failed to send reply to {recipient}: {e}")
        raise

def move_to_junk(mail, email_id):
    try:
        mail.store(email_id, "+X-GM-LABELS", "Spam")
        mail.store(email_id, "+FLAGS", "(\\Deleted)")
        mail.expunge()
        logging.info(f"Moved email {email_id} to Junk")
    except Exception as e:
        logging.error(f"Failed to move email {email_id} to Junk: {e}")
        raise

def flag_email(mail, email_id):
    try:
        mail.store(email_id, "+FLAGS", "(\\Flagged)")
        logging.info(f"Flagged email {email_id}")
    except Exception as e:
        logging.error(f"Failed to flag email {email_id}: {e}")
        raise

def process_emails(num_emails=5):
    try:
        mail = connect_to_inbox(EMAIL_USER, EMAIL_PASS)
        emails = fetch_emails(mail, num_emails)

        for email in emails:
            category = classify_email(email)
            print(f"Email from {email['from']} classified as: {category}")

            if category == "Reply":
                reply_text = generate_reply(email)
                send_reply(EMAIL_USER, EMAIL_PASS, email["from"], email["subject"], reply_text)
                action_msg = Runner.run_sync(action_agent, f"Category: Reply\nEmail ID: {email['id']}")
                print(action_msg.final_output.strip())
            elif category == "Junk":
                move_to_junk(mail, email["id"])
                action_msg = Runner.run_sync(action_agent, f"Category: Junk\nEmail ID: {email['id']}")
                print(action_msg.final_output.strip())
            elif category == "Maybe":
                flag_email(mail, email["id"])
                action_msg = Runner.run_sync(action_agent, f"Category: Maybe\nEmail ID: {email['id']}")
                print(action_msg.final_output.strip())
            else:
                logging.warning(f"Unknown category '{category}' for email {email['id']}")

        mail.logout()
        logging.info("Email processing completed.")
    except Exception as e:
        logging.error(f"Processing failed: {e}")
        raise

if __name__ == "__main__":
    process_emails(num_emails=5)
