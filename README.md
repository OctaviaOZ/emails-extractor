# Smart Job Tracker

A modern, AI-powered dashboard to track your job applications using data from Gmail.

## Features
- **Auto-Sync:** Fetches emails from your 'apply' label.
- **AI Extraction:** Uses OpenAI (optional) to intelligently parse status, company, and next steps from unstructured email bodies.
- **Heuristic Fallback:** Robust rule-based extraction if no API key is provided.
- **Dashboard:** Visual funnel (Applied -> Interview -> Offer) and timeline.
- **Local DB:** Stores data in a local SQLite database (`database.db`).

## Setup

1. **Install Dependencies:**
   ```bash
   cd smart-job-tracker
   poetry install
   ```

2. **Credentials:**
   It should be the `credentials.json` in the folder, ensure the app can find it.
   

3. **Run:**
   ```bash
   poetry run streamlit run app/ui.py
   ```

## Configuration
- **OpenAI:** Enter your API Key in the sidebar settings for better accuracy.
- **Gmail Label:** Default is 'apply'. Change in `app/ui.py` if needed.
