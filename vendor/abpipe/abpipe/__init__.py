"""abpipe: an EPUB to m4b audiobook pipeline with local neural TTS and whisper QC.

Read CONTRACT.md before you change anything in this package.
"""

__version__ = "0.1.0"

STAGES = (
    "extract",
    "normalize",
    "chunk",
    "render",
    "qc",
    "assemble",
    "bind",
    "deliver",
)

STAGE_DIRS = {
    "extract": "01-extract",
    "normalize": "02-normalize",
    "chunk": "03-chunks",
    "render": "04-audio",
    "qc": "05-qc",
    "assemble": "06-chapters",
    "bind": "07-book",
}
