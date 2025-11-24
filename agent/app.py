"""
Agent API ASGI Application
LangChain agent with MCP tool support, using Azure OpenAI
Runs with uvicorn on Azure Container Apps
"""
import json
import logging
import os
from pathlib import Path

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain.agents import create_agent
from langchain_openai import ChatOpenAI
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load system instructions once at module level
system_instructions_path = Path(__file__).parent / "instructions.txt"
with open(system_instructions_path, 'r') as f:
    system_prompt = f.read().strip()

# Configuration from environment
openai_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip('/')
if openai_endpoint and not openai_endpoint.endswith('/openai/v1'):
    openai_endpoint = f"{openai_endpoint}/openai/v1"

openai_deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5-mini")

mcp_server_url = os.getenv("MCP_SERVER_URL", "http://localhost:3000").rstrip('/')
if not mcp_server_url.endswith('/mcp'):
    mcp_server_url = f"{mcp_server_url}/mcp"

async def chat_ui_endpoint(request):
    """Serve the chat UI."""
    try:
        html_path = Path(__file__).parent / 'static' / 'index.html'
        return FileResponse(html_path, media_type='text/html')
    except Exception as e:
        logger.error(f"Error loading chat UI: {e}", exc_info=True)
        return JSONResponse(
            {"error": f"Error loading chat UI: {str(e)}"},
            status_code=500
        )


async def chat_endpoint(request):
    """
    Chat endpoint for the agent with streaming support.
    
    Request body:
    {
        "message": "user message",
        "history": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
    }
    """
    try:
        # Parse request body
        req_body = await request.json()
        message = req_body.get("message")
        history = req_body.get("history", [])
        
        if not message:
            return JSONResponse(
                {"error": "message is required"},
                status_code=400
            )

        # Initialize Azure credential and token provider
        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential,
            "https://cognitiveservices.azure.com/.default"
        )

        
        # Create ChatOpenAI model with Responses API and all available tools
        # Build list of tools including MCP, web search, image generation, and code interpreter
        tools = [
            # Remote MCP server for Zava sales data
            {
                "type": "mcp",
                "server_label": "zava-sales",
                "server_url": mcp_server_url,
                "require_approval": "never"
            },
            # Web search for real-time information
            {
                "type": "web_search_preview"
            },
            # Image generation capability
            {
                "type": "image_generation",
                "quality": "low"  # Can be "low", "medium", "high", or "auto"
            },
            # Code interpreter for data analysis and calculations
            {
                "type": "code_interpreter",
                "container": {"type": "auto"}  # Creates new container automatically
            }
        ]
        
        model = ChatOpenAI(
            model=openai_deployment,
            base_url=openai_endpoint,
            api_key=token_provider,
            temperature=0.7,
            streaming=True,
            use_responses_api=True,
            include=["code_interpreter_call.outputs"]  # Include code interpreter outputs (images, files)
        ).bind_tools(tools)
        
        # Create agent
        agent = create_agent(
            model=model,
            tools=[],  # Tools are bound via MCP
            system_prompt=system_prompt
        )
        
        # Build messages for agent
        messages = []
        
        # Add history
        if history:
            for msg in history:
                messages.append({
                    "role": msg["role"],
                    "content": msg["content"]
                })
        
        # Add current message
        messages.append({
            "role": "user",
            "content": message
        })
        
        # Async generator for true streaming
        async def generate_stream():
            """Stream chunks as they arrive from the agent."""
            full_response = ""
            images = []
            
            # Stream with stream_mode="messages" to get token-by-token output
            async for token, metadata in agent.astream(
                {"messages": messages},
                stream_mode="messages"
            ):
                # Extract content from the token
                if hasattr(token, 'content'):
                    content = token.content
                    
                    # Handle content_blocks (new API format with include=["code_interpreter_call.outputs"])
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                # Text content block
                                if block.get('type') == 'text':
                                    text = block.get('text', '')
                                    if text:
                                        full_response += text
                                        yield json.dumps({"chunk": text}) + "\n"
                                # Image content block from code interpreter or image generation
                                elif block.get('type') == 'image':
                                    image_data = {
                                        "base64": block.get('base64', ''),
                                        "format": block.get('format', 'png')
                                    }
                                    images.append(image_data)
                                    yield json.dumps({"image": image_data}) + "\n"
                            elif hasattr(block, 'text'):
                                # Object with text attribute
                                text = block.text
                                if text:
                                    full_response += text
                                    yield json.dumps({"chunk": text}) + "\n"
                    # Handle simple string content
                    elif isinstance(content, str) and content:
                        full_response += content
                        yield json.dumps({"chunk": content}) + "\n"
                
                # Also check for content_blocks attribute directly on the message
                if hasattr(token, 'content_blocks'):
                    for block in token.content_blocks:
                        if isinstance(block, dict) and block.get('type') == 'image':
                            image_data = {
                                "base64": block.get('base64', ''),
                                "format": block.get('format', 'png')
                            }
                            images.append(image_data)
                            yield json.dumps({"image": image_data}) + "\n"
            
            # Send final complete message
            yield json.dumps({
                "message": full_response,
                "role": "assistant",
                "images": images,
                "done": True
            }) + "\n"
        
        return StreamingResponse(
            generate_stream(),
            media_type="application/json"
        )
    
    except ValueError as e:
        logger.error(f"ValueError in chat endpoint: {e}", exc_info=True)
        return JSONResponse(
            {"error": str(e)},
            status_code=400
        )
    except Exception as e:
        logger.error(f"Error in chat endpoint: {e}", exc_info=True)
        return JSONResponse(
            {"error": f"Internal server error: {str(e)}"},
            status_code=500
        )


async def health_endpoint(request):
    """Health check endpoint."""
    try:
        return JSONResponse({
            "status": "healthy",
            "openai_endpoint": openai_endpoint,
            "mcp_server": mcp_server_url
        })
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return JSONResponse(
            {"status": "unhealthy", "error": str(e)},
            status_code=503
        )


# Define routes
routes = [
    Route("/", chat_ui_endpoint, methods=["GET"]),
    Route("/api/chat", chat_endpoint, methods=["POST"]),
    Route("/api/health", health_endpoint, methods=["GET"]),
]

# Create Starlette app
app = Starlette(debug=False, routes=routes)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
