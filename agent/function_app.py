"""
Azure Functions Agent API
LangChain agent with MCP tool support, using Azure OpenAI
"""
import json
import logging
import os

import azure.functions as func
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load system instructions once at module level
system_instructions_path = os.path.join(os.path.dirname(__file__), "instructions.txt")
with open(system_instructions_path, 'r') as f:
    system_prompt = f.read().strip()

# Configuration from environment make sure using v1
openai_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip('/')
if openai_endpoint and not openai_endpoint.endswith('/openai/v1'):
    openai_endpoint = f"{openai_endpoint}/openai/v1"

openai_deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5-mini")

mcp_server_url = os.getenv("MCP_SERVER_URL", "http://localhost:3000").rstrip('/')
if not mcp_server_url.endswith('/mcp'):
    mcp_server_url = f"{mcp_server_url}/mcp"


# Azure Functions app
app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


@app.function_name(name="chat_ui")
@app.route(route="chat", methods=["GET"])
async def chat_ui_endpoint(req: func.HttpRequest) -> func.HttpResponse:
    """Serve the chat UI."""
    try:
        html_path = os.path.join(os.path.dirname(__file__), 'static', 'index.html')
        with open(html_path, 'r') as f:
            html_content = f.read()
        return func.HttpResponse(
            body=html_content,
            mimetype="text/html"
        )
    except Exception as e:
        return func.HttpResponse(
            body=f"Error loading chat UI: {str(e)}",
            status_code=500
        )


@app.function_name(name="chat")
@app.route(route="chat", methods=["POST"])
async def chat_endpoint(req: func.HttpRequest) -> func.HttpResponse:
    """
    Chat endpoint for the agent.
    
    Request body:
    {
        "message": "user message",
        "history": [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
    }
    """
    try:
        # Parse request body
        req_body = req.get_json()
        message = req_body.get("message")
        history = req_body.get("history", [])
        
        if not message:
            return func.HttpResponse(
                body=json.dumps({"error": "message is required"}),
                status_code=400,
                mimetype="application/json"
            )
        
        # Setup Azure authentication
        credential = DefaultAzureCredential()
        token_provider = get_bearer_token_provider(
            credential,
            "https://cognitiveservices.azure.com/.default"
        )
        
        # Create ChatOpenAI model with Responses API and Remote MCP
        mcp_tool = {
            "type": "mcp",
            "server_label": "zava-sales",
            "server_url": mcp_server_url,
            "require_approval": "never"
        }
        
        model = ChatOpenAI(
            model=openai_deployment,
            base_url=openai_endpoint,
            api_key=token_provider,
            temperature=0.7,
            streaming=True,
            use_responses_api=True
        ).bind_tools([mcp_tool])
        
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
        
        # Collect streaming chunks from agent
        chunks = []
        final_content = ""
        
        async for event in agent.astream_events({"messages": messages}, version="v2"):
            event_type = event.get("event")
            data = event.get("data", {})
            
            # Stream text chunks from the model
            if event_type == "on_chat_model_stream":
                chunk = data.get("chunk", {})
                if hasattr(chunk, "content") and chunk.content:
                    # Handle both string and list content
                    if isinstance(chunk.content, str):
                        content_text = chunk.content
                    elif isinstance(chunk.content, list) and len(chunk.content) > 0:
                        # Extract text from content blocks
                        content_text = chunk.content[0].get("text", "") if isinstance(chunk.content[0], dict) else str(chunk.content[0])
                    else:
                        content_text = str(chunk.content)
                    
                    if content_text:
                        final_content += content_text
                        chunks.append(json.dumps({"chunk": content_text}) + "\n")
            
            # Get final output when chain completes
            elif event_type == "on_chat_model_end":
                output = data.get("output", {})
                if hasattr(output, "content") and output.content:
                    # Extract just the text content from the message
                    if isinstance(output.content, list):
                        # Find the text content block
                        for content_block in output.content:
                            if isinstance(content_block, dict) and content_block.get("type") == "text":
                                final_content = content_block.get("text", final_content)
                                break
                    elif isinstance(output.content, str):
                        final_content = output.content
        
        # Send final complete message with just the text content
        chunks.append(json.dumps({
            "message": final_content,
            "role": "assistant",
            "done": True
        }) + "\n")
        
        return func.HttpResponse(
            body=''.join(chunks),
            mimetype="application/json",
            status_code=200
        )
    
    except ValueError as e:
        logger.error(f"ValueError in chat endpoint: {e}", exc_info=True)
        return func.HttpResponse(
            body=json.dumps({"error": str(e)}),
            status_code=400,
            mimetype="application/json"
        )
    except Exception as e:
        logger.error(f"Error in chat endpoint: {e}", exc_info=True)
        return func.HttpResponse(
            body=json.dumps({"error": f"Internal server error: {str(e)}"}),
            status_code=500,
            mimetype="application/json"
        )


@app.function_name(name="health")
@app.route(route="health", methods=["GET"])
async def health_endpoint(req: func.HttpRequest) -> func.HttpResponse:
    """Health check endpoint."""
    try:
        return func.HttpResponse(
            body=json.dumps({
                "status": "healthy",
                "openai_endpoint": openai_endpoint,
                "mcp_server": mcp_server_url
            }),
            mimetype="application/json"
        )
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return func.HttpResponse(
            body=json.dumps({
                "status": "unhealthy",
                "error": str(e)
            }),
            status_code=503,
            mimetype="application/json"
        )
