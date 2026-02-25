"""
Tests for HTTP Header Security utilities
"""
import pytest
from collegue.core.header_security import (
    sanitize_header_value,
    sanitize_header_name,
    validate_url_safe_string,
    HeaderSecurityError
)


class TestHeaderSecurity:
    """Test cases for header security functions."""
    
    def test_sanitize_header_value_normal(self):
        """Test that normal header values are unchanged."""
        assert sanitize_header_value("Bearer token123") == "Bearer token123"
        assert sanitize_header_value("application/json") == "application/json"
        assert sanitize_header_value("simple-value") == "simple-value"
    
    def test_sanitize_header_value_crlf_detection(self):
        """Test that CRLF characters are detected and raise errors."""
        # CR injection
        with pytest.raises(HeaderSecurityError, match="CR"):
            sanitize_header_value("token\rX-Injected: value")
        
        # LF injection
        with pytest.raises(HeaderSecurityError, match="LF"):
            sanitize_header_value("token\nX-Injected: value")
        
        # CRLF injection
        with pytest.raises(HeaderSecurityError, match="CR|LF"):
            sanitize_header_value("token\r\nX-Injected: value")
    
    def test_sanitize_header_value_null_byte(self):
        """Test that null bytes are detected."""
        with pytest.raises(HeaderSecurityError, match="NULL"):
            sanitize_header_value("token\x00after-null")
    
    def test_sanitize_header_name_valid(self):
        """Test that valid header names are preserved."""
        assert sanitize_header_name("Content-Type") == "Content-Type"
        assert sanitize_header_name("X-Custom-Header") == "X-Custom-Header"
        assert sanitize_header_name("Authorization") == "Authorization"
    
    def test_sanitize_header_name_invalid(self):
        """Test that invalid characters are removed from header names."""
        # Spaces should be removed
        assert sanitize_header_name("X Header") == "XHeader"
        
        # Newlines should be removed
        assert sanitize_header_name("X\nHeader") == "XHeader"
        
        # Special characters should be removed
        assert sanitize_header_name("X@Header#") == "XHeader"
    
    def test_validate_url_safe_string_normal(self):
        """Test that normal URL strings pass validation."""
        assert validate_url_safe_string("/path/to/resource") == "/path/to/resource"
        assert validate_url_safe_string("query-param") == "query-param"
    
    def test_validate_url_safe_string_space(self):
        """Test that spaces are rejected in URL strings."""
        with pytest.raises(HeaderSecurityError, match="space"):
            validate_url_safe_string("/path with spaces")
    
    def test_validate_url_safe_string_crlf(self):
        """Test that CRLF is rejected in URL strings."""
        with pytest.raises(HeaderSecurityError, match="CR|LF"):
            validate_url_safe_string("/path\r\n/injected")
    
    def test_validate_url_safe_string_at_sign(self):
        """Test that @ is rejected (prevents userinfo injection)."""
        with pytest.raises(HeaderSecurityError, match="at-sign"):
            validate_url_safe_string("user@host/path")
    
    def test_sanitize_header_value_control_chars(self):
        """Test that control characters are removed."""
        # Control characters (other than CR/LF/NULL) are silently removed
        assert sanitize_header_value("token\x01") == "token"  # SOH removed
        assert sanitize_header_value("token\x1f") == "token"  # US removed
    
    def test_non_string_input(self):
        """Test that non-string inputs are handled."""
        # Numbers should be converted to strings
        assert sanitize_header_value(12345) == "12345"
        
        # Should work with None (converted to string)
        assert sanitize_header_value(None) == "None"
