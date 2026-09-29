"""Application entry point with a deliberately broken import."""

from utils.helper import greet


def run() -> str:
    return greet()
