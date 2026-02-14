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

# Max topic slugs to query posts for
MAX_TOPIC_SLUGS = 5

# GraphQL query to find topic slugs matching a search term
TOPICS_QUERY = """
query FindTopics($query: String!, $first: Int!) {
  topics(query: $query, first: $first) {
    edges {
      node {
        slug
        name
        postsCount
      }
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


def _find_topic_slugs(access_token: str, query: str) -> List[str]:
    """Find Product Hunt topic slugs matching a search term.

    The PH API's posts query filters by topic slug, not free text.
    This step converts a user's search term into matching topic slugs.

    Returns:
        List of topic slugs sorted by postsCount (most active first)
    """
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
                "query": TOPICS_QUERY,
                "variables": {"query": query, "first": MAX_TOPIC_SLUGS},
            },
            timeout=15,
        )
    except http.HTTPError as e:
        _log_error(f"Topic search error: {e}")
        return []

    if "errors" in response:
        for err in response.get("errors", []):
            _log_error(f"Topic query error: {err.get('message', str(err))}")
        return []

    edges = response.get("data", {}).get("topics", {}).get("edges", [])
    # Sort by postsCount descending so we query the most active topics first
    topics = []
    for edge in edges:
        node = edge.get("node", {})
        if node.get("slug"):
            topics.append((node["slug"], node.get("postsCount", 0)))
    topics.sort(key=lambda t: t[1], reverse=True)

    slugs = [t[0] for t in topics]
    if slugs:
        _log_info(f"Found topic slugs: {', '.join(slugs)}")
    else:
        _log_info(f"No matching topic slugs for '{query}'")
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

    Two-step process: first finds topic slugs matching the search term,
    then queries posts for each matching topic slug within the date range.

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
