"""OpenAI-compatible API tests."""


class TestOpenAICompatibleAPI:
    """Test OpenAI-compatible endpoints.

    Note: These endpoints may not be available if WITH_OPENAI_API=false
    or if no LLM is configured.
    """

    def test_list_models(self, api_client):
        """Test listing models - may not be available without LLM."""
        response = api_client.get("/v1/models")
        # 200 if available, 404 if WITH_OPENAI_API=false or no LLM configured
        assert response.status_code in [200, 404]
        if response.status_code == 200:
            data = response.json()
            assert "data" in data
            assert data["object"] == "list"

    def test_chat_completions_endpoint(self, api_client):
        """Test chat completions endpoint exists or is disabled."""
        response = api_client.post("/v1/chat/completions", json={"model": "openrag-all", "messages": []})
        # 404 if endpoint disabled, 400/422 if enabled but invalid input
        assert response.status_code in [400, 404, 422]

    def test_chat_completions_invalid_model(self, api_client):
        """Test chat completions with invalid model."""
        response = api_client.post(
            "/v1/chat/completions",
            json={
                "model": "nonexistent-model",
                "messages": [{"role": "user", "content": "Hello"}],
            },
        )
        # Should return error for invalid model, 404 if endpoint disabled
        assert response.status_code in [400, 404, 422]

    def test_completions_endpoint(self, api_client):
        """Test completions endpoint exists or is disabled."""
        response = api_client.post("/v1/completions", json={"model": "openrag-all", "prompt": "Test"})
        # 404 if endpoint disabled, other codes if enabled
        assert response.status_code in [200, 400, 404, 422, 500]

    def test_chat_completions_exceeds_token_limit(self, api_client):
        """When requested tokens exceed model limit, expect HTTP 413 (or 404 if disabled)."""
        very_long_content = "test " * 20000
        payload = {
            "model": "", 
            "messages": [{"role": "user", "content": very_long_content}],
            "max_tokens": 100000,
        }
        response = api_client.post("/v1/chat/completions", json=payload)
        assert response.status_code in [413, 404]
        if response.status_code == 413:
            body = response.json()
            assert "exceeds maximum token limit" in body.get("detail", "").lower()

    def test_completions_exceeds_token_limit(self, api_client):
        """When requested tokens exceed model limit, expect HTTP 413 (or 404 if disabled)."""
        very_long_prompt = "test " * 20000
        payload = {
            "model": "",  
            "prompt": very_long_prompt,
            "max_tokens": 100000,
        }
        response = api_client.post("/v1/completions", json=payload)
        assert response.status_code in [413, 404]
        if response.status_code == 413:
            body = response.json()
            assert "exceeds maximum token limit" in body.get("detail", "").lower()

