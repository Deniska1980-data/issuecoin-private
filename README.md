# issuecoin-private
Private OpenShift + n8n Edition of IssueCoin (AI Expense Tracker)

issuecoin-private/
├── app_private.py          # Private edition of IssueCoin (OpenShift + n8n)
├── .env.example            # Example environment variables
├── README.md
├── requirements.txt
├── /data/
│   └── groceries.xlsx
└── /integrations/
    ├── n8n_connector.py
    ├── google_sheets.py
    ├── google_drive.py
    └── whisper_stt.py
    
## Installation

1. Clone the repository  
   ```bash
   git clone https://github.com/Deniska1980-data/issuecoin-private.git
   cd issuecoin-private

pip install -r requirements.txt

cp .env.example .env

python app_private.py
