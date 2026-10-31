"""The troubleshooting assistant: ingestion, the graph, the questions and the API.

Importing this package sets one switch for the spreadsheet library, so the committed
workbook is always written with the same bytes.
"""

import os

os.environ.setdefault("OPENPYXL_LXML", "False")
