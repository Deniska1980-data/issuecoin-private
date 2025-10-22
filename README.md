# issuecoin-private
Private OpenShift + n8n Edition of IssueCoin (AI Expense Tracker)

issuecoin-private/
├── app.py               # tvoja pôvodná verzia
├── app_private.py        # nová verzia s integráciami
├── /data/
│   └── groceries.xlsx
├── /integrations/
│   ├── n8n_connector.py
│   ├── google_sheets.py
│   ├── google_drive.py
│   └── whisper_stt.py
└── README.md
