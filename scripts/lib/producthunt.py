"""Product Hunt API v2 (GraphQL) client for product discovery."""

import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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

# GraphQL query for searching posts
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


def search_producthunt(
    access_token: str,
    topic: str,
    from_date: str,
    to_date: str,
    depth: str = "default",
    mock_response: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Search Product Hunt for relevant products.

    Args:
        access_token: Product Hunt API v2 access token
        topic: Search topic
        from_date: Start date (YYYY-MM-DD)
        to_date: End date (YYYY-MM-DD)
        depth: Research depth - "quick", "default", or "deep"
        mock_response: Mock response for testing

    Returns:
        Raw API response with product data
    """
    if mock_response is not None:
        return mock_response

    first = DEPTH_CONFIG.get(depth, DEPTH_CONFIG["default"])

    # Convert dates to ISO 8601 format for Product Hunt API
    posted_after = f"{from_date}T00:00:00Z"
    posted_before = f"{to_date}T23:59:59Z"

    variables = {
        "topic": topic,
        "postedAfter": posted_after,
        "postedBefore": posted_before,
        "first": first,
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        response = http.request(
            "POST",
            PH_API_URL,
            headers=headers,
            json_data={
                "query": POSTS_QUERY,
                "variables": variables,
            },
            timeout=30,
        )
    except http.HTTPError as e:
        _log_error(f"Product Hunt API error: {e}")
        return {"error": str(e)}

    return response


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
