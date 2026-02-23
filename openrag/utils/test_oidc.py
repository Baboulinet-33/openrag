from utils.oidc import OIDCValidator


class TestIsJwt:
    def test_valid_jwt_structure(self):
        # Three base64 segments separated by dots
        token = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature"
        assert OIDCValidator.is_jwt(token) is True

    def test_opaque_token(self):
        token = "or-abc123def456abc123def456abc12345"
        assert OIDCValidator.is_jwt(token) is False

    def test_two_segments(self):
        token = "part1.part2"
        assert OIDCValidator.is_jwt(token) is False

    def test_empty_string(self):
        assert OIDCValidator.is_jwt("") is False

    def test_four_segments(self):
        token = "a.b.c.d"
        assert OIDCValidator.is_jwt(token) is False
