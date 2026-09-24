# The editor as a server.  Same code as the local app; the difference is entirely in
# the environment variables below.
FROM python:3.12-slim

# tesseract does the OCR.  The font packages matter more than they look: matching the
# font of existing text means drawing candidates and comparing them, so the editor can
# only match what is installed.  These are the metric-compatible clones of the fonts
# PDFs actually name - Liberation and Croscore for Arial/Times/Courier, URW for the
# PostScript families, Noto for everything else.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-eng \
        fonts-liberation fonts-croscore fonts-dejavu-core fonts-urw-base35 fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pdfeditor ./pdfeditor
COPY static ./static
COPY run.py ./

# what makes this a server rather than a desktop tool
ENV PDF_EDITOR_BIND=0.0.0.0 \
    PDF_EDITOR_BEHIND_PROXY=1 \
    PDF_EDITOR_MAX_UPLOAD_MB=50 \
    PDF_EDITOR_SESSIONS_PER_VISITOR=4 \
    PDF_EDITOR_SESSIONS_TOTAL=200 \
    PDF_EDITOR_SESSION_IDLE_MINUTES=30 \
    PDF_EDITOR_MAX_OCR_PAGES=20 \
    PYTHONUNBUFFERED=1

EXPOSE 8765
CMD ["python", "run.py", "--port", "8765", "--no-browser"]
