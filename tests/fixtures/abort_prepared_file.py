import os
import sys
from pathlib import Path

from dagoberd.safe_files import prepared_new_file

with prepared_new_file(Path(sys.argv[1])) as staged:
    staged.write_bytes(b"not-a-real-pdf")
    os._exit(23)
