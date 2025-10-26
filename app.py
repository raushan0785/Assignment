from flask import Flask, request, render_template, Response
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse
import os, json, requests
from io import BytesIO
import gspread
from google.oauth2 import service_account

# File parsing
import PyPDF2
import docx
try:
    import pytesseract
    from PIL import Image
except ImportError:
    pytesseract = None

# Gemini client
from google import genai
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

# ===== Load Environment Variables =====
load_dotenv()
ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP = os.getenv("TWILIO_WHATSAPP_NUMBER")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")


# Gemini Client
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# Google Sheets using credentials file
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
credentials = service_account.Credentials.from_service_account_file(
    "/etc/secrets/google-key.json", scopes=SCOPES
)
gc = gspread.authorize(credentials)
sheet = gc.open("candidatedata").sheet1

# Twilio Client
client = Client(ACCOUNT_SID, AUTH_TOKEN)

# Flask App
app = Flask(__name__)
received_messages = []  # store messages in memory for UI

# ===== Helper Functions =====
def extract_text_from_file(media_url, media_type):
    """Extract readable text from PDF, DOCX, or image file."""
    try:
        response = requests.get(media_url, auth=HTTPBasicAuth(ACCOUNT_SID, AUTH_TOKEN))
        if 'pdf' in media_type.lower():
            pdf_reader = PyPDF2.PdfReader(BytesIO(response.content))
            text = ""
            for page in pdf_reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text += page_text + "\n"
            return text
        elif 'word' in media_type.lower():
            doc = docx.Document(BytesIO(response.content))
            return "\n".join([p.text for p in doc.paragraphs])
        elif media_type.startswith("image/") and pytesseract:
            img = Image.open(BytesIO(response.content))
            return pytesseract.image_to_string(img)
        else:
            return ""
    except Exception as e:
        print("❌ File parse error:", e)
        return ""


def extract_details(text):
    """Use Gemini to extract multiple people details from the given text."""
    prompt = (
        "You are a strict data extractor.\n"
        "Extract *all* people’s details (Name, Email, Phone number) from the given text.\n"
        "If there are multiple people, include each one as a separate JSON object.\n"
        "Return *only* a JSON array like this:\n"
        "[{\"Name\": \"Alice\", \"Email\": \"alice@example.com\", \"Phone\": \"+911234567890\"},"
        "{\"Name\": \"Bob\", \"Email\": \"bob@example.com\", \"Phone\": \"9876543210\"}]\n\n"
        f"Text:\n{text}"
    )

    try:
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt
        )
        data = response.text.strip()

        # Extract valid JSON part
        start = data.find("[")
        end = data.rfind("]") + 1
        if start == -1 or end == 0:
            raise ValueError("No JSON array detected")

        json_str = data[start:end].replace("'", '"')
        details_list = json.loads(json_str)

        cleaned = []
        for d in details_list:
            cleaned.append({
                "Name": d.get("Name", "").strip(),
                "Email": d.get("Email", "").strip(),
                "Phone": d.get("Phone", "").strip()
            })

        return cleaned if cleaned else [{"Name": "", "Email": "", "Phone": ""}]
    except Exception as e:
        print("❌ Gemini parsing error:", e)
        return [{"Name": "", "Email": "", "Phone": ""}]

# ===== Flask Routes =====
@app.route("/incoming", methods=["POST"])
def incoming_message():
    from_number = request.form.get("From")
    message_body = request.form.get("Body") or ""
    num_media = int(request.form.get("NumMedia", 0))

    # If a media file is attached
    if num_media > 0:
        media_url = request.form.get("MediaUrl0")
        media_type = request.form.get("MediaContentType0")
        message_body = extract_text_from_file(media_url, media_type)

    # Extract candidate details
    people = extract_details(message_body)

    count = 0
    for person in people:
        name = person.get("Name", "")
        email = person.get("Email", "")
        phone = person.get("Phone", "")
        if not phone:
            phone = ""
        if name or email or phone:
            try:
                sheet.append_row([name, email, phone])
                count += 1
            except Exception as e:
                print("Sheet append error:", e)
            received_messages.append({"Name": name, "Email": email, "Phone": phone})

    # Twilio WhatsApp reply
    resp = MessagingResponse()
    if count > 1:
        resp.message(f"✅ {count} candidates’ details have been stored successfully.")
    elif count == 1:
        resp.message("✅ 1 candidate’s details have been stored successfully.")
    else:
        resp.message("⚠️ No valid candidate details found in your file or message.")

    return str(resp)


# ===== SSE for dynamic UI =====
@app.route("/stream")
def stream():
    """Live stream of newly received candidate data for frontend."""
    def event_stream():
        last_len = 0
        while True:
            if len(received_messages) > last_len:
                new_data = received_messages[last_len:]
                last_len = len(received_messages)
                yield f"data: {json.dumps(new_data)}\n\n"
    return Response(event_stream(), mimetype="text/event-stream")


@app.route("/")
def index():
    return render_template("index.html")


# ===== Run Flask =====
if __name__ == "__main__":
    app.run(port=5000, debug=True)



