import os
from dotenv import load_dotenv
from local_db import local_client

load_dotenv()

# We provide the local client powered by the ingested datasets and pipeline results
supabase = local_client
