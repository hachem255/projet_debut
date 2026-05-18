"""
semantic_search package — SemanticSearch + DatabaseFactory.
"""
from semantic_search.search import SemanticSearch, FAISS_INDEX_PATH
from semantic_search.database_factory import DatabaseFactory

__all__ = ["SemanticSearch", "DatabaseFactory", "FAISS_INDEX_PATH"]