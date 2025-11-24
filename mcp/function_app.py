"""
Azure Functions MCP Server
Exposes MCP tools via HTTP endpoint for Streamable HTTP transport
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Annotated, Any, Optional

import asyncpg
import azure.functions as func
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from fastmcp import FastMCP
from openai import AzureOpenAI
from pydantic import Field

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Create MCP server instance
mcp = FastMCP("Zava Sales Analysis Tools", stateless_http=True)

# Global providers  
db_provider: Optional['PostgreSQLProvider'] = None
embedding_provider: Optional['SemanticSearchEmbedding'] = None

# Global Starlette app state
starlette_app = None
lifespan_started = False


async def ensure_app_lifespan():
    """Ensure the Starlette app and its lifespan are initialized."""
    global starlette_app, lifespan_started
    
    if starlette_app is None:
        # Create the Starlette app with lifespan
        starlette_app = mcp.streamable_http_app()
        logger.info("Created Starlette app from FastMCP")
    
    if not lifespan_started:
        # The app has a lifespan context manager that needs to be entered
        # to initialize the session manager's task group
        # We'll do this by manually calling the lifespan startup
        try:
            # Get lifespan from the app
            if hasattr(starlette_app.router, 'lifespan_context'):
                # Create the lifespan context
                lifespan_cm = starlette_app.router.lifespan_context(starlette_app)
                # Enter the context (this calls startup events)
                await lifespan_cm.__aenter__()
                logger.info("✅ Starlette lifespan started - session manager task group initialized")
            lifespan_started = True
        except Exception as e:
            logger.error(f"Error starting lifespan: {e}")
            # Continue anyway - might work without explicit lifespan
            lifespan_started = True


class PostgreSQLProvider:
    """PostgreSQL database provider with pgvector support."""
    
    def __init__(self, connection_url: str):
        self.connection_url = connection_url
        self.pool: Optional[asyncpg.Pool] = None
    
    async def connect(self):
        """Create connection pool."""
        try:
            self.pool = await asyncpg.create_pool(
                self.connection_url,
                min_size=1,
                max_size=10
            )
            logger.info("✅ PostgreSQL connection pool established")
        except Exception as e:
            logger.error(f"❌ Failed to connect to PostgreSQL: {e}")
            raise
    
    async def close(self):
        """Close connection pool."""
        if self.pool:
            await self.pool.close()
            logger.info("Connection pool closed")
    
    async def execute_query(self, query: str) -> list[dict]:
        """Execute a query and return results."""
        if not self.pool:
            await self.connect()
        
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query)
            return [dict(row) for row in rows]
    
    async def get_table_schemas(self) -> str:
        """Get detailed schema information for all tables."""
        if not self.pool:
            await self.connect()
        
        schema_query = """
        SELECT 
            table_name,
            column_name,
            data_type,
            is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position;
        """
        
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(schema_query)
            
        # Format schema information
        schemas = {}
        for row in rows:
            table = row['table_name']
            if table not in schemas:
                schemas[table] = []
            schemas[table].append({
                'column': row['column_name'],
                'type': row['data_type'],
                'nullable': row['is_nullable']
            })
        
        return json.dumps(schemas, indent=2)


class SemanticSearchEmbedding:
    """Semantic search using Azure OpenAI embeddings and pgvector."""
    
    def __init__(self, endpoint: str, deployment: str):
        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential,
            "https://cognitiveservices.azure.com/.default"
        )
        
        self.client = AzureOpenAI(
            azure_endpoint=endpoint,
            azure_ad_token_provider=token_provider,
            api_version="2024-02-01"
        )
        self.deployment = deployment
        logger.info(f"✅ Embedding provider initialized with deployment: {deployment}")
    
    def get_embedding(self, text: str) -> list[float]:
        """Get embedding vector for text."""
        response = self.client.embeddings.create(
            model=self.deployment,
            input=text
        )
        return response.data[0].embedding
    
    async def search_products(self, query: str, max_rows: int = 5, threshold: float = 0.7) -> str:
        """Perform semantic search on products using pgvector."""
        global db_provider
        if not db_provider or not db_provider.pool:
            return "Database not connected"
        
        try:
            # Get embedding for search query
            query_embedding = self.get_embedding(query)
            embedding_str = '[' + ','.join(str(x) for x in query_embedding) + ']'
            
            # Perform vector similarity search
            search_query = f"""
            SELECT 
                product_id,
                product_name,
                category,
                description,
                price,
                1 - (embedding <=> '{embedding_str}'::vector) as similarity
            FROM products
            WHERE 1 - (embedding <=> '{embedding_str}'::vector) > {threshold}
            ORDER BY embedding <=> '{embedding_str}'::vector
            LIMIT {max_rows};
            """
            
            results = await db_provider.execute_query(search_query)
            
            if not results:
                return "No products found matching the search criteria."
            
            # Format results
            formatted_results = []
            for row in results:
                formatted_results.append(
                    f"Product: {row['product_name']}\n"
                    f"Category: {row['category']}\n"
                    f"Price: ${row['price']:.2f}\n"
                    f"Description: {row['description']}\n"
                    f"Similarity: {row['similarity']:.2%}\n"
                )
            
            return "\n---\n".join(formatted_results)
        
        except Exception as e:
            logger.error(f"Error in semantic search: {e}")
            return f"Error performing search: {str(e)}"


# MCP Tools
@mcp.tool()
def get_current_utc_date() -> str:
    """Get the current UTC date and time.
    
    Returns:
        Current UTC timestamp in ISO format
    """
    return datetime.now(timezone.utc).isoformat()


@mcp.tool()
async def get_table_schemas() -> str:
    """Get the schema information for all database tables.
    
    Returns:
        JSON string containing table schemas with columns, types, and constraints
    """
    global db_provider
    if not db_provider:
        return "Database not configured"
    
    try:
        return await db_provider.get_table_schemas()
    except Exception as e:
        logger.error(f"Error getting schemas: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
async def execute_sales_query(
    query: Annotated[str, Field(description="SQL query to execute against the sales database")]
) -> str:
    """Execute a SQL query against the sales database.
    
    Args:
        query: SQL query to execute (SELECT statements only)
    
    Returns:
        JSON string containing query results
    """
    global db_provider
    if not db_provider:
        return "Database not configured"
    
    # Security: Only allow SELECT queries
    if not query.strip().upper().startswith('SELECT'):
        return "Error: Only SELECT queries are allowed"
    
    try:
        results = await db_provider.execute_query(query)
        return json.dumps(results, indent=2, default=str)
    except Exception as e:
        logger.error(f"Error executing query: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
async def semantic_search_products(
    query: Annotated[str, Field(description="Search query to find relevant products")],
    max_rows: Annotated[int, Field(description="Maximum number of results to return", ge=1, le=20)] = 5,
    threshold: Annotated[float, Field(description="Similarity threshold (0-1)", ge=0, le=1)] = 0.7
) -> str:
    """Search for products using semantic similarity with pgvector.
    
    Args:
        query: Natural language search query
        max_rows: Maximum number of results (1-20)
        threshold: Minimum similarity score (0-1)
    
    Returns:
        Formatted list of matching products with similarity scores
    """
    global embedding_provider
    if not embedding_provider:
        return "Semantic search not configured - Azure OpenAI endpoint not set"
    
    try:
        return await embedding_provider.search_products(query, max_rows, threshold)
    except Exception as e:
        logger.error(f"Error in semantic search: {e}")
        return f"Error: {str(e)}"


async def initialize_providers():
    """Initialize database and embedding providers."""
    global db_provider, embedding_provider
    
    # Initialize PostgreSQL provider (lazy connection)
    postgres_url = os.getenv("POSTGRES_URL")
    if postgres_url:
        db_provider = PostgreSQLProvider(postgres_url)
        logger.info("✅ Database provider initialized (will connect on first use)")
    else:
        logger.warning("⚠️  POSTGRES_URL not set - database tools will not work")
    
    # Initialize embedding provider
    openai_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    embedding_deployment = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-small")
    
    if openai_endpoint:
        try:
            embedding_provider = SemanticSearchEmbedding(openai_endpoint, embedding_deployment)
            logger.info("✅ Embedding provider initialized")
        except Exception as e:
            logger.error(f"Failed to initialize embeddings: {e}")
            embedding_provider = None
    else:
        logger.warning("⚠️  AZURE_OPENAI_ENDPOINT not set - semantic search will not work")


# Azure Functions app
app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


@app.function_name(name="mcp")
@app.route(route="mcp", methods=["POST"])
async def mcp_endpoint(req: func.HttpRequest) -> func.HttpResponse:
    """MCP endpoint for Streamable HTTP transport."""
    
    # Initialize providers on first request
    global db_provider, embedding_provider
    
    if db_provider is None and embedding_provider is None:
        await initialize_providers()
    
    # Ensure the Starlette app lifespan is started
    await ensure_app_lifespan()
    
    try:
        # Convert Azure Functions request to ASGI scope
        scope = {
            "type": "http",
            "method": req.method,
            "path": "/mcp",
            "query_string": req.url.split("?", 1)[1].encode() if "?" in req.url else b"",
            "headers": [(k.encode(), v.encode()) for k, v in req.headers.items()],
        }
        
        # Get request body
        body = req.get_body()
        
        async def receive():
            return {"type": "http.request", "body": body}
        
        response_started = False
        response_status = 200
        response_headers = []
        response_body = []
        
        async def send(message):
            nonlocal response_started, response_status, response_headers, response_body
            
            if message["type"] == "http.response.start":
                response_started = True
                response_status = message["status"]
                response_headers = message.get("headers", [])
            elif message["type"] == "http.response.body":
                response_body.append(message.get("body", b""))
        
        # Call the Starlette app directly (it will handle the request)
        # The lifespan will be managed automatically when the app starts
        await starlette_app(scope, receive, send)
        
        # Build response
        headers = {k.decode(): v.decode() for k, v in response_headers}
        body_bytes = b"".join(response_body)
        
        return func.HttpResponse(
            body=body_bytes,
            status_code=response_status,
            headers=headers
        )
    
    except Exception as e:
        logger.error(f"Error processing MCP request: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return func.HttpResponse(
            body=json.dumps({"error": str(e)}),
            status_code=500,
            mimetype="application/json"
        )


@app.function_name(name="health")
@app.route(route="health", methods=["GET"])
async def health_check(req: func.HttpRequest) -> func.HttpResponse:
    """Health check endpoint."""
    return func.HttpResponse(
        body=json.dumps({
            "status": "healthy",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "database": "configured" if db_provider else "not configured",
            "embeddings": "configured" if embedding_provider else "not configured"
        }),
        mimetype="application/json"
    )
