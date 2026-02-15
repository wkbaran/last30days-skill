"""Product Hunt API v2 (GraphQL) client for product discovery."""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import http


def _log_error(msg: str):
    """Log error to stderr."""
    sys.stderr.write(f"[PH ERROR] {msg}\n")
    sys.stderr.flush()


def _log_info(msg: str):
    """Log info to stderr."""
    sys.stderr.write(f"[PH] {msg}\n")
    sys.stderr.flush()


# Product Hunt API v2 (GraphQL)
PH_API_URL = "https://api.producthunt.com/v2/api/graphql"

# Depth configurations: number of results to request
DEPTH_CONFIG = {
    "quick": 10,
    "default": 20,
    "deep": 50,
}

# Max topic slugs to query posts for
MAX_TOPIC_SLUGS = 5

# Cache settings
CACHE_MAX_AGE = 86400  # 24 hours in seconds
_CACHE_DIR_OVERRIDE = os.environ.get('LAST30DAYS_CONFIG_DIR')
if _CACHE_DIR_OVERRIDE == "":
    _CACHE_DIR = None
elif _CACHE_DIR_OVERRIDE:
    _CACHE_DIR = Path(_CACHE_DIR_OVERRIDE)
else:
    _CACHE_DIR = Path.home() / ".config" / "last30days"
CACHE_FILE = _CACHE_DIR / "ph_topics_cache.json" if _CACHE_DIR else None

# GraphQL query to fetch all topics (paginated)
ALL_TOPICS_QUERY = """
query($first: Int!, $after: String) {
  topics(first: $first, after: $after, order: FOLLOWERS_COUNT) {
    edges {
      node {
        slug
        name
        postsCount
      }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

# GraphQL query for posts within a topic slug
POSTS_QUERY = """
query SearchPosts($topic: String!, $postedAfter: DateTime!, $postedBefore: DateTime!, $first: Int!) {
  posts(
    topic: $topic
    postedAfter: $postedAfter
    postedBefore: $postedBefore
    first: $first
    order: VOTES
  ) {
    edges {
      node {
        id
        name
        tagline
        url
        votesCount
        commentsCount
        website
        createdAt
        topics {
          edges {
            node {
              name
            }
          }
        }
        makers {
          name
          username
        }
      }
    }
  }
}
"""


# ---------------------------------------------------------------------------
# Topic cache: fetch all PH topics, cache locally, refresh daily
# ---------------------------------------------------------------------------

def _fetch_all_topics(access_token: str) -> List[Dict[str, Any]]:
    """Fetch all Product Hunt topics via paginated GraphQL queries."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    all_topics = []
    cursor = None

    while True:
        variables = {"first": 50}
        if cursor:
            variables["after"] = cursor

        try:
            response = http.request(
                "POST",
                PH_API_URL,
                headers=headers,
                json_data={"query": ALL_TOPICS_QUERY, "variables": variables},
                timeout=30,
            )
        except http.HTTPError as e:
            _log_error(f"Failed to fetch topics: {e}")
            break

        if "errors" in response:
            for err in response.get("errors", []):
                _log_error(f"Topics fetch error: {err.get('message', str(err))}")
            break

        data = response.get("data", {}).get("topics", {})
        for edge in data.get("edges", []):
            node = edge.get("node", {})
            if node.get("slug"):
                all_topics.append({
                    "slug": node["slug"],
                    "name": node.get("name", ""),
                    "posts": node.get("postsCount", 0),
                })

        page_info = data.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    return all_topics


def _load_cache() -> Optional[Dict[str, Any]]:
    """Load topic cache from disk if it exists and is fresh."""
    if not CACHE_FILE or not CACHE_FILE.exists():
        return None

    try:
        with open(CACHE_FILE, "r") as f:
            cache = json.load(f)
        if time.time() - cache.get("timestamp", 0) < CACHE_MAX_AGE:
            return cache
    except (json.JSONDecodeError, OSError, KeyError):
        pass

    return None


def _save_cache(topics: List[Dict[str, Any]]):
    """Save topic list to disk cache."""
    if not CACHE_FILE:
        return

    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump({"timestamp": time.time(), "topics": topics}, f)
    except OSError as e:
        _log_error(f"Failed to write topic cache: {e}")


def _get_topics(access_token: str) -> List[Dict[str, Any]]:
    """Get all PH topics, using cache when fresh."""
    cache = _load_cache()
    if cache:
        return cache["topics"]

    _log_info("Refreshing topic cache...")
    topics = _fetch_all_topics(access_token)
    if topics:
        _save_cache(topics)
    return topics


# ---------------------------------------------------------------------------
# Local topic matching: map free-text query to relevant topic slugs
# ---------------------------------------------------------------------------

# Expand abbreviations/shorthand in the query before matching.
# Keys are patterns found in user queries; values are what they mean
# in PH topic vocabulary. Applied as whole-word replacements.
_EXPANSIONS = {
    "ai": "artificial intelligence",
    "ml": "machine learning",
    "ux": "user experience",
    "ui": "user interface",
    "iot": "internet of things",
    "vr": "virtual reality",
    "ar": "augmented reality",
    "crypto": "cryptocurrency",
    "defi": "decentralized finance",
    "nft": "nfts",
    "seo": "seo",
    "crm": "crm",
    "hr": "human resources",
    "nocode": "no code",
    "no-code": "no code",
    "devtools": "developer tools",
    "fintech": "fintech",
    "edtech": "education technology",
    "healthtech": "health technology",
    "ecommerce": "e-commerce",
    "e-commerce": "e-commerce",
    "cli": "command line tools",
    "llm": "artificial intelligence",
    "gpt": "artificial intelligence",
    "opensource": "open source",
    "open-source": "open source",
    "saas": "saas",
    "api": "api",
}

# Multi-word expansions applied to the full query string.
# Checked before single-word expansions so "machine learning" isn't
# split into individual word lookups.
_PHRASE_EXPANSIONS = {
    "machine learning": "artificial intelligence",
    "deep learning": "artificial intelligence",
    "social media": "social media",
    "growth hacking": "growth hacking",
}


def _expand_query(query: str) -> str:
    """Expand abbreviations in the query to match PH topic names."""
    result = query.lower()

    # Apply multi-word phrase expansions first
    for phrase, expansion in _PHRASE_EXPANSIONS.items():
        result = result.replace(phrase, expansion)

    # Apply single-word expansions
    words = result.split()
    expanded = []
    for w in words:
        clean = w.strip(".,!?")
        if clean in _EXPANSIONS:
            expanded.append(_EXPANSIONS[clean])
        else:
            expanded.append(clean)
    return " ".join(expanded)


def _phrase_match_score(phrase_words: List[str], query_words: List[str]) -> float:
    """Check if phrase words appear in query words, return match score.

    Matches whole words only. Consecutive matches (phrase appears intact)
    score higher than scattered matches. Returns 0 for no match.
    """
    if not phrase_words:
        return 0

    phrase_len = len(phrase_words)

    # Check for consecutive (exact phrase) match first
    for i in range(len(query_words) - phrase_len + 1):
        if query_words[i:i + phrase_len] == phrase_words:
            # Exact phrase match - score by phrase length (longer = better)
            return phrase_len * 2.0

    # Check if all words appear (non-consecutive)
    if all(w in query_words for w in phrase_words):
        return phrase_len * 1.0

    # Single-word topics: require exact word match in query
    if phrase_len == 1 and phrase_words[0] in query_words:
        return 1.0

    return 0


def _match_topics(query: str, topics: List[Dict[str, Any]]) -> List[str]:
    """Match a free-text query to relevant topic slugs.

    Reversed matching: checks if each topic's name appears within the
    query string, rather than splitting the query into words. This avoids
    false positives from individual word matches (e.g. "tools" matching
    "design-tools" when the query is about video).

    Strategy:
    1. Expand abbreviations in query (ai -> artificial intelligence)
    2. For each topic, check if its name appears in the expanded query
    3. Score by match length and postsCount, return top matches
    """
    expanded = _expand_query(query)

    candidates: Dict[str, float] = {}
    posts_count: Dict[str, int] = {}

    for t in topics:
        posts_count[t["slug"]] = t.get("posts", 0)

    # Tokenize expanded query for word-boundary matching
    query_words = expanded.split()

    for t in topics:
        slug = t["slug"]
        name_lower = t["name"].lower()
        # Normalize hyphens to spaces so "No-Code" matches "no code"
        name_normalized = name_lower.replace("-", " ")
        name_words = name_normalized.split()

        # Check if all words of the topic name appear in the query
        score = _phrase_match_score(name_words, query_words)
        if score > 0:
            candidates[slug] = score

    if not candidates:
        return []

    # Sort by match length (desc), break ties by postsCount (desc)
    ranked = sorted(
        candidates.items(),
        key=lambda item: (item[1], posts_count.get(item[0], 0)),
        reverse=True,
    )

    slugs = [slug for slug, _ in ranked[:MAX_TOPIC_SLUGS]]
    return slugs


def _find_topic_slugs(access_token: str, query: str) -> List[str]:
    """Find Product Hunt topic slugs matching a search term.

    Uses a locally cached topic list with programmatic matching
    instead of the PH topics API (which has poor multi-word support).

    Returns:
        List of topic slugs, best matches first
    """
    topics = _get_topics(access_token)
    if not topics:
        _log_error("No topics available (cache empty, fetch failed)")
        return []

    slugs = _match_topics(query, topics)
    if slugs:
        _log_info(f"Matched topics: {', '.join(slugs)}")
    else:
        _log_info(f"No matching topics for '{query}'")
    return slugs


def search_producthunt(
    access_token: str,
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    mock_response: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Search Product Hunt for relevant products.

    Two-step process: matches the search term to topic slugs using a
    locally cached topic list, then queries posts for each matching slug.

    Args:
        access_token: Product Hunt API v2 access token
        topic: Search topic (free text)
        from_date: Start date (YYYY-MM-DD)
        to_date: End date (YYYY-MM-DD)
        depth: Research depth - "quick", "default", or "deep"
        mock_response: Mock response for testing

    Returns:
        Combined API response with product data from all matching topics
    """
    if mock_response is not None:
        return mock_response

    # Step 1: Find topic slugs matching the search term
    slugs = _find_topic_slugs(access_token, topic)
    if not slugs:
        return {"data": {"posts": {"edges": []}}}

    first = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["default"])
    posted_after = f"{from_date}T00:00:00Z"
    posted_before = f"{to_date}T23:59:59Z"

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    # Step 2: Query posts for each topic slug, merge results
    all_edges = []
    seen_ids = set()

    for slug in slugs:
        try:
            response = http.request(
                "POST",
                PH_API_URL,
                headers=headers,
                json_data={
                    "query": POSTS_QUERY,
                    "variables": {
                        "topic": slug,
                        "postedAfter": posted_after,
                        "postedBefore": posted_before,
                        "first": first,
                    },
                },
                timeout=30,
            )
        except http.HTTPError as e:
            _log_error(f"Posts query error for topic '{slug}': {e}")
            continue

        if "errors" in response:
            for err in response.get("errors", []):
                _log_error(f"Posts query error: {err.get('message', str(err))}")
            continue

        edges = response.get("data", {}).get("posts", {}).get("edges", [])
        for edge in edges:
            post_id = edge.get("node", {}).get("id")
            if post_id and post_id not in seen_ids:
                seen_ids.add(post_id)
                all_edges.append(edge)

    # Sort merged results by votes descending
    all_edges.sort(
        key=lambda e: e.get("node", {}).get("votesCount", 0),
        reverse=True,
    )

    # Trim to requested depth
    all_edges = all_edges[:first]

    return {"data": {"posts": {"edges": all_edges}}}


def parse_ph_response(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Parse Product Hunt GraphQL response to extract product items.

    Args:
        response: Raw API response from Product Hunt

    Returns:
        List of item dicts in normalized format
    """
    items = []

    # Handle error responses
    if "error" in response and response["error"]:
        error = response["error"]
        if isinstance(error, dict):
            _log_error(f"Product Hunt API error: {error.get('message', str(error))}")
        else:
            _log_error(f"Product Hunt API error: {error}")
        return items

    # Handle GraphQL errors
    if "errors" in response:
        for err in response["errors"]:
            _log_error(f"GraphQL error: {err.get('message', str(err))}")
        return items

    # Extract posts from GraphQL response
    posts_data = response.get("data", {}).get("posts", {})
    edges = posts_data.get("edges", [])

    for i, edge in enumerate(edges):
        node = edge.get("node", {})
        if not node:
            continue

        name = node.get("name", "")
        if not name:
            continue

        # Parse date
        created_at = node.get("createdAt", "")
        date_str = None
        if created_at:
            try:
                dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                date_str = dt.date().isoformat()
            except (ValueError, TypeError):
                pass

        # Get engagement metrics
        votes = node.get("votesCount", 0)
        comments = node.get("commentsCount", 0)

        # Build engagement
        engagement = {
            "votes": votes,
            "comments": comments,
        }

        # Extract topics
        topics = []
        for topic_edge in node.get("topics", {}).get("edges", []):
            topic_name = topic_edge.get("node", {}).get("name", "")
            if topic_name:
                topics.append(topic_name)

        # Extract makers
        makers = []
        for maker in node.get("makers", []):
            maker_name = maker.get("name", "")
            if maker_name:
                makers.append(maker_name)

        # Product Hunt URL (ph post page)
        ph_url = node.get("url", "")
        # Website URL (the actual product)
        website = node.get("website", "")

        result_item = {
            "id": f"PH{i+1}",
            "name": name,
            "tagline": node.get("tagline", ""),
            "url": ph_url,
            "website": website,
            "date": date_str,
            "engagement": engagement,
            "topics": topics,
            "makers": makers,
            "why_relevant": _build_relevance_reason(votes, comments, topics, makers),
            "relevance": _estimate_relevance(i, votes, comments),
        }

        items.append(result_item)

    return items


def _build_relevance_reason(
    votes: int,
    comments: int,
    topics: List[str],
    makers: List[str],
) -> str:
    """Build a human-readable relevance reason."""
    parts = []
    if votes:
        parts.append(f"{votes} upvotes")
    if comments:
        parts.append(f"{comments} comments")
    if topics:
        parts.append(f"topics: {', '.join(topics[:3])}")
    if makers:
        parts.append(f"by {makers[0]}")

    return ", ".join(parts) if parts else "Product Hunt launch"


def _estimate_relevance(rank: int, votes: int, comments: int) -> float:
    """Estimate relevance from rank and engagement.

    Product Hunt returns results ordered by votes, so rank matters.
    """
    # Base relevance from rank (ordered by votes)
    base = max(0.3, 1.0 - (rank * 0.03))

    # Engagement boost
    if votes > 500 or comments > 50:
        base = min(1.0, base + 0.1)
    elif votes > 100 or comments > 20:
        base = min(1.0, base + 0.05)

    return round(min(1.0, base), 2)
