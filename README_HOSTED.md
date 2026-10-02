# Supplier Profile Database — Hosted Version

This is the hosted one-link version of the Supplier Profile Database.

Features:
- Product-wise database
- Reusable Supplier Master
- Many suppliers ↔ many products
- Factory visits
- AI Brochure Review
- AI Website Review
- Catalogue + Website review
- Product-wise catalogue photo extraction

Current storage:
Product and supplier records are stored in the browser localStorage, so they are tied to the browser/device. AI review and extracted photo files run on the server. A later cloud-database upgrade can centralize records for multi-device use.

Render:
Build: pip install -r requirements.txt
Start: gunicorn app:app
Environment variable: OPENAI_API_KEY
Optional: OPENAI_MODEL=gpt-5.1

AI Review accepts:
- Catalogue only
- Website only
- Catalogue + Website

Never put the OpenAI API key into the HTML. Keep it server-side.


Website-only AI review uses OpenAI's hosted web_search tool, so a supplier site that blocks direct server crawling can still be reviewed when public pages are searchable.


PHOTO IMPORT: PDF catalogue pages are visually analyzed. The system first uses AI photo regions, then falls back to embedded PDF images, and finally to a rendered page image so a visual is still attached when extraction is possible.
