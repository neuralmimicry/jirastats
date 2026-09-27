import os
import logging
import base64
import mimetypes
from typing import Optional, List, Dict, Any
from io import BytesIO

# Third-party libraries
try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    from docx import Document
except ImportError:
    Document = None

try:
    from odf import text, teletype
    from odf.opendocument import load
except ImportError:
    load = None

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

from llm_providers import LLMProvider

logger = logging.getLogger(__name__)

class FileConverter:
    """
    Utility to convert various file formats into usable text information.
    """
    def __init__(self, llm: Optional[LLMProvider] = None, llm_params: Optional[Dict[str, Any]] = None):
        self.llm = llm
        self.llm_params = llm_params or {}

    def convert(self, file_path: str, mime_type: Optional[str] = None) -> str:
        """
        Main entry point to convert a file to text.
        """
        if not os.path.exists(file_path):
            return f"Error: File not found at {file_path}"

        if not mime_type:
            mime_type, _ = mimetypes.guess_type(file_path)

        ext = os.path.splitext(file_path)[1].lower()
        
        logger.info(f"Converting file: {file_path} (MIME: {mime_type}, Ext: {ext})")

        try:
            if ext in (".txt", ".md"):
                content = self._read_text(file_path)
            elif ext == ".pdf":
                content = self._read_pdf(file_path)
            elif ext == ".docx":
                content = self._read_docx(file_path)
            elif ext in (".odf", ".odt"):
                content = self._read_odf(file_path)
            elif ext == ".html" or (mime_type and "html" in mime_type):
                content = self._read_html(file_path)
            elif ext in (".jpg", ".jpeg", ".png", ".svg") or (mime_type and "image" in mime_type):
                content = self._describe_image(file_path, mime_type)
            elif ext in (".mp3", ".mp4") or (mime_type and ("audio" in mime_type or "video" in mime_type)):
                content = self._transcribe_audio(file_path)
            else:
                # Fallback to plain text if unknown but maybe readable
                content = self._read_text(file_path)
            
            logger.debug(f"Converted content (first 500 chars): {content[:500]}...")
            return content
        except Exception as e:
            logger.error(f"Failed to convert {file_path}: {e}")
            return f"Error converting {file_path}: {str(e)}"

    def _read_text(self, file_path: str) -> str:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

    def _read_pdf(self, file_path: str) -> str:
        if not PdfReader:
            return "Error: pypdf not installed."
        reader = PdfReader(file_path)
        text_content = []
        for page in reader.pages:
            text_content.append(page.extract_text() or "")
        return "\n".join(text_content)

    def _read_docx(self, file_path: str) -> str:
        if not Document:
            return "Error: python-docx not installed."
        doc = Document(file_path)
        return "\n".join([para.text for para in doc.paragraphs])

    def _read_odf(self, file_path: str) -> str:
        if not load:
            return "Error: odfpy not installed."
        textdoc = load(file_path)
        all_text = []
        # ODF structure is complex, teletype.extractText is a good helper
        from odf import teletype
        # Find all text:p and text:h
        # But teletype.extractText(textdoc.body) might be simpler
        return teletype.extractText(textdoc.body)

    def _read_html(self, file_path: str) -> str:
        if not BeautifulSoup:
            return self._read_text(file_path)
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            soup = BeautifulSoup(f, "html.parser")
            # Remove scripts and styles
            for script in soup(["script", "style"]):
                script.decompose()
            return soup.get_text(separator="\n", strip=True)

    def _describe_image(self, file_path: str, mime_type: str) -> str:
        if not self.llm:
            return f"Image file: {os.path.basename(file_path)} (No LLM available for description)"
        
        logger.info(f"Using LLM to describe image: {file_path}")
        with open(file_path, "rb") as f:
            b64_data = base64.b64encode(f.read()).decode("utf-8")
        
        if not mime_type:
            mime_type = "image/jpeg"

        prompt = (
            "Describe this image in detail, focusing on information relevant to technical research. "
            "Extract any visible text, describe diagrams, charts, or key visual elements. "
            "Use British English."
        )
        
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{b64_data}"}
                    }
                ]
            }
        ]
        
        try:
            resp = self.llm.predict(
                messages, 
                system="You are a conservative, reserved British professional technical writer and assistant. Your task is to describe images for research with strict factual accuracy and factually-based descriptions. Use a formal, non-sycophantic tone and British English.",
                **self.llm_params
            )
            return f"--- Image Description: {os.path.basename(file_path)} ---\n{resp.text}"
        except Exception as e:
            logger.error(f"Image description failed: {e}")
            return f"Error describing image {os.path.basename(file_path)}: {str(e)}"

    def _transcribe_audio(self, file_path: str) -> str:
        if not self.llm:
            return f"Audio/Video file: {os.path.basename(file_path)} (No LLM available for transcription)"
        
        logger.info(f"Transcribing audio/video: {file_path}")
        try:
            transcript = self.llm.transcribe(file_path, timeout=self.llm_params.get("timeout"))
            return f"--- Transcription: {os.path.basename(file_path)} ---\n{transcript}"
        except NotImplementedError:
            return f"Transcription not supported by current LLM provider ({self.llm.name})."
        except Exception as e:
            logger.error(f"Transcription failed: {e}")
            return f"Error transcribing {os.path.basename(file_path)}: {str(e)}"
