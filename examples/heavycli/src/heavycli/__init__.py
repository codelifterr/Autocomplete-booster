"""Stands in for an SDK whose import pulls in requests, botocore, pydantic, ..."""
import os
import time

time.sleep(float(os.environ.get("HEAVYCLI_IMPORT_SECONDS", "0.5")))
