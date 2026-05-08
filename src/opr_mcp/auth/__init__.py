"""Discord OAuth integration for opr-mcp.

When OPR_MCP_AUTH_ENABLED=true, the FastMCP server runs as an OAuth 2.1
authorization server (per the MCP spec) that delegates user identity to
Discord and gates token issuance on Discord guild membership.
"""
