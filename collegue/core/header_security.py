"""
HTTP Header Security Utilities

Provides functions to sanitize HTTP headers and prevent injection attacks.
"""
import re
from typing import Optional


class HeaderSecurityError(Exception):
    """Exception raised when a header security violation is detected."""
    pass


def sanitize_header_value(value: str, field_name: str = "header") -> str:
    """
    Sanitize a string to be safe for use as an HTTP header value.
    
    Removes characters that could be used for CRLF injection attacks:
    - Carriage return (\r)
    - Line feed (\n)
    - Other control characters (0x00-0x1f, 0x7f)
    - Null bytes
    
    Args:
        value: The header value to sanitize
        field_name: Name of the field for error messages
        
    Returns:
        Sanitized header value safe for use in HTTP headers
        
    Raises:
        HeaderSecurityError: If dangerous characters are detected and removed
    """
    if not isinstance(value, str):
        value = str(value)
    
    # Check for dangerous characters
    dangerous_chars = []
    if '\r' in value:
        dangerous_chars.append('CR (\\r)')
    if '\n' in value:
        dangerous_chars.append('LF (\\n)')
    if '\x00' in value:
        dangerous_chars.append('NULL (\\x00)')
    
    # Remove CRLF characters (primary defense against header injection)
    sanitized = value.replace('\r', '').replace('\n', '')
    
    # Remove null bytes
    sanitized = sanitized.replace('\x00', '')
    
    # Remove other control characters
    sanitized = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', sanitized)
    
    # If we removed dangerous characters, log a security event
    if dangerous_chars:
        # In a real scenario, you'd want to log this as a security event
        # For now, we'll raise an error to be strict
        raise HeaderSecurityError(
            f"Header injection attempt detected in {field_name}: "
            f"contained {', '.join(dangerous_chars)}. "
            f"Value has been sanitized but this indicates a potential attack."
        )
    
    return sanitized


def sanitize_header_name(name: str) -> str:
    """
    Sanitize an HTTP header name.
    
    Header names should only contain alphanumeric characters and hyphens.
    
    Args:
        name: The header name to sanitize
        
    Returns:
        Sanitized header name
    """
    # Header names should only contain alphanumeric and hyphen
    # Convert to string and remove invalid characters
    name = str(name)
    
    # Remove any characters that aren't valid in header names
    sanitized = re.sub(r'[^a-zA-Z0-9\-_]', '', name)
    
    return sanitized


def validate_url_safe_string(value: str, field_name: str = "value") -> str:
    """
    Validate that a string is safe for use in URLs.
    
    Prevents injection of URL control characters.
    
    Args:
        value: The string to validate
        field_name: Name of the field for error messages
        
    Returns:
        The validated string
        
    Raises:
        HeaderSecurityError: If dangerous characters are found
    """
    if not isinstance(value, str):
        value = str(value)
    
    # Check for characters that could break URL structure
    dangerous_patterns = [
        (' ', 'space'),
        ('\r', 'CR'),
        ('\n', 'LF'),
        ('\x00', 'NULL'),
        ('@', 'at-sign (userinfo injection)'),
    ]
    
    found_issues = []
    for char, description in dangerous_patterns:
        if char in value:
            found_issues.append(description)
    
    if found_issues:
        raise HeaderSecurityError(
            f"Unsafe characters detected in {field_name}: {', '.join(found_issues)}"
        )
    
    return value
