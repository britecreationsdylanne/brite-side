"""
Claude (Anthropic) API Client
For content generation using Claude models
"""

import os
import time
from anthropic import Anthropic


class ClaudeClient:
    """Client for Claude API"""

    def __init__(self, api_key=None):
        """Initialize Claude client"""
        self.api_key = api_key or os.getenv('ANTHROPIC_API_KEY')
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY not found in environment")

        self.client = Anthropic(api_key=self.api_key)
        self.default_model = "claude-opus-4-5-20251101"  # Claude Opus 4.5 (frontier model for writing)

    def generate_content(
        self,
        prompt: str,
        system_prompt: str = None,
        temperature: float = 0.7,
        max_tokens: int = 2000,
        model: str = None
    ) -> dict:
        """
        Generate content using Claude

        Args:
            prompt: User prompt
            system_prompt: System instructions
            temperature: Creativity (0-1)
            max_tokens: Max response length
            model: Model to use (defaults to claude-3-5-sonnet)

        Returns:
            dict with content, model, tokens, cost_estimate, latency_ms
        """
        start_time = time.time()

        model_name = model or self.default_model

        # Build messages
        messages = [{"role": "user", "content": prompt}]

        # Call Claude API
        response = self.client.messages.create(
            model=model_name,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt if system_prompt else "",
            messages=messages
        )

        end_time = time.time()
        latency_ms = int((end_time - start_time) * 1000)

        # Extract content
        content = response.content[0].text

        # Calculate tokens
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        total_tokens = input_tokens + output_tokens

        # Estimate cost
        cost_estimate = self._estimate_cost(model_name, input_tokens, output_tokens)

        return {
            "content": content,
            "model": model_name,
            "tokens": total_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_estimate": f"${cost_estimate:.4f}",
            "latency_ms": latency_ms
        }

    def _estimate_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """Estimate cost based on model pricing"""

        # Claude 3.5 Sonnet pricing (as of 2025)
        if "sonnet" in model.lower():
            input_cost = (input_tokens / 1_000_000) * 3.00  # $3 per 1M input tokens
            output_cost = (output_tokens / 1_000_000) * 15.00  # $15 per 1M output tokens
        # Claude 3 Haiku (cheaper option)
        elif "haiku" in model.lower():
            input_cost = (input_tokens / 1_000_000) * 0.25
            output_cost = (output_tokens / 1_000_000) * 1.25
        # Claude 3 Opus (premium)
        elif "opus" in model.lower():
            input_cost = (input_tokens / 1_000_000) * 15.00
            output_cost = (output_tokens / 1_000_000) * 75.00
        else:
            input_cost = (input_tokens / 1_000_000) * 3.00
            output_cost = (output_tokens / 1_000_000) * 15.00

        return input_cost + output_cost

    def search_web(self, query: str, max_results: int = 5) -> list:
        """
        Search web using Claude's native web search tool

        Args:
            query: Search query
            max_results: Maximum number of results to return

        Returns:
            List of search results with title, description, url
        """
        try:
            # Use Claude with web search tool enabled
            search_prompt = f"""Find {max_results} recent, relevant articles about: {query}

For each article, provide:
- Title
- Brief description (1-2 sentences)
- Source URL
- How recent it is (e.g., "2 days ago")

Return as JSON array with this structure:
[
  {{"title": "...", "description": "...", "url": "...", "age": "..."}},
  ...
]"""

            response = self.client.messages.create(
                model=self.default_model,
                max_tokens=2000,
                temperature=0.3,
                messages=[{"role": "user", "content": search_prompt}],
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 3
                }]
            )

            # Extract web search results from the response
            results = []

            # Iterate through response content to find web_search_tool_result blocks
            for block in response.content:
                if block.type == "web_search_tool_result":
                    # Extract search results
                    for result in block.content:
                        if hasattr(result, 'url') and hasattr(result, 'title'):
                            results.append({
                                "title": result.title,
                                "description": getattr(result, 'snippet', '')[:200] if hasattr(result, 'snippet') else '',
                                "url": result.url,
                                "age": getattr(result, 'page_age', '') if hasattr(result, 'page_age') else ''
                            })

            # If we found results from web search, return them
            if results:
                return results[:max_results]

            # Fallback: Try to parse JSON from text blocks if no structured results
            for block in response.content:
                if block.type == "text" and block.text:
                    try:
                        content = block.text
                        # Extract JSON from markdown code blocks if present
                        if "```json" in content:
                            content = content.split("```json")[1].split("```")[0].strip()
                        elif "```" in content:
                            content = content.split("```")[1].split("```")[0].strip()

                        import json
                        parsed = json.loads(content)
                        if isinstance(parsed, list):
                            return parsed[:max_results]
                    except:
                        continue

            return []

        except Exception as e:
            print(f"Claude web search error: {e}")
            import traceback
            traceback.print_exc()
            return []

    def search_wedding_news(self, month: str) -> list:
        """Search for wedding venue industry news"""
        query = f"wedding venue industry news statistics data trends 2025 2026 {month}"
        return self.search_web(query, max_results=5)

    def search_wedding_tips(self, month: str) -> list:
        """Search for wedding venue management tips"""
        query = f"wedding venue marketing tips advice strategies 2025 2026 {month}"
        return self.search_web(query, max_results=5)

    def search_wedding_trends(self, month: str, season: str) -> list:
        """Search for seasonal wedding trends"""
        query = f"wedding trends {season} 2025 2026 venue decor planning {month}"
        return self.search_web(query, max_results=5)
