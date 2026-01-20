import json
from pathlib import Path
from urllib.parse import quote

import consts
from components.pipeline import RagPipeline
from config import load_config
from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.documents.base import Document
from langchain_openai import ChatOpenAI
from openai import AsyncOpenAI
from models.openai import (
    OpenAIChatCompletionRequest,
    OpenAICompletionRequest,
)
from utils.dependencies import get_vectordb
from utils.logger import get_logger

from .utils import (
    check_llm_model_availability,
    current_user,
    current_user_or_admin_partitions,
    current_user_or_admin_partitions_list,
    get_partition_name,
    truncate,
)

logger = get_logger()
config = load_config()
router = APIRouter()

ragpipe = RagPipeline(config=config)


@router.get(
    "/models",
    summary="OpenAI-compatible model listing endpoint",
    description="""List available models in OpenAI-compatible format.

**Available Models:**
- `openrag-{partition_name}`: Query a specific partition
- `openrag-all`: Query across all accessible partitions

**Response Format:**
Returns models in OpenAI-compatible format with:
- `id`: Model identifier
- `object`: Always "model"
- `created`: Creation timestamp
- `owned_by`: Always "OpenRAG"

**Note:** Only partitions you have access to will be listed.
""",
    response_description="A list of available models in OpenAI format",
)
async def list_models(
    _: None = Depends(check_llm_model_availability),
    vectordb=Depends(get_vectordb),
    user_partitions=Depends(current_user_or_admin_partitions),
):
    if [p["partition"] for p in user_partitions] == ["all"]:
        user_partitions = await vectordb.list_partitions.remote()
    logger.debug("Listing models", partition_count=len(user_partitions))

    models = []
    for partition in user_partitions:
        model_id = f"{consts.PARTITION_PREFIX}{partition['partition']}"
        models.append(
            {
                "id": model_id,
                "object": "model",
                "created": partition["created_at"],
                "owned_by": "OpenRAG",
            }
        )

    models.append(
        {
            "id": f"{consts.PARTITION_PREFIX}all",
            "object": "model",
            "created": 0,
            "owned_by": "OpenRAG",
        }
    )
    return JSONResponse(content={"object": "list", "data": models})


def __prepare_sources(request: Request, docs: list[Document]):
    links = []
    for doc in docs:
        doc_metadata = dict(doc.metadata)
        filename = Path(doc_metadata.get("source")).name
        file_url = str(request.url_for("static", path=filename))
        encoded_url = quote(file_url, safe=":/")
        links.append(
            {
                "file_url": encoded_url,
                "chunk_url": str(request.url_for("get_extract", extract_id=doc_metadata["_id"])),
                **doc_metadata,
            }
        )
    return links


def is_direct_llm_model(
    request: OpenAIChatCompletionRequest | OpenAICompletionRequest,
) -> bool:
    """Check if request should use direct LLM (no RAG partition).

    Returns True if model is None, empty, or matches the configured default model.
    """
    return request.model is None or request.model == "" or request.model == config.llm.get("model")


async def get_max_model_tokens(model_id: str | None) -> int:
    """Retrieve the maximum model token limit from vLLM's OpenAI server.

    - Queries `/v1/models` and looks for `max_model_len` for the given `model_id`.
    - Falls back to `config.llm.max_tokens` or 8192 if unavailable.
    """
    default_limit = int(config.llm_context.get("max_llm_context_size", 8192))
    try:
        client = AsyncOpenAI(base_url=config.llm["base_url"], api_key=config.llm["api_key"])
        resp = await client.models.list()
        
        for m in getattr(resp, "data", []) or []:
            mid = getattr(m, "id", None)
            try:
                md = m.model_dump()
            except Exception:                
                md = None

            if not mid and md and isinstance(md, dict):
                mid = md.get("id")

            if model_id and mid != model_id:
                continue

            max_len = getattr(m, "max_model_len", None)
            if max_len is None and hasattr(m, "model_extra"):
                extra = getattr(m, "model_extra", {}) or {}
                max_len = extra.get("max_model_len")
            if max_len is None and md and isinstance(md, dict):
                max_len = md.get("max_model_len")

            if isinstance(max_len, (int, float)) and max_len > 0:
                logger.debug("Fetched max_model_len from vLLM", model=mid, max_model_len=int(max_len))
                return int(max_len)

        logger.debug("max_model_len not found in /v1/models response, using default", default=default_limit)
        return default_limit
    except Exception as e:
        logger.warning("Failed to query /v1/models for max_model_len; using default", error=str(e))
        return default_limit


def validate_tokens_limit(
    request: OpenAIChatCompletionRequest | OpenAICompletionRequest,
    max_tokens_allowed: int | None = None,
) -> tuple[bool, str]:
    """Validate if the request respects the maximum token limit.
    
    Args:
        request: The OpenAI request object
        max_tokens_allowed: Maximum allowed tokens for the request.
                          If None, retrieves from config.
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    try:
        llm = ChatOpenAI(**config.llm)
        _length_function = llm.get_num_tokens
        
        if max_tokens_allowed is None:
            max_tokens_allowed = config.llm.get("max_tokens", 4096)
        
        if isinstance(request, OpenAIChatCompletionRequest):
            message_tokens = sum(
                _length_function(m.content) + 4  
                for m in request.messages
            )
            requested_tokens = request.max_tokens or 1024
            total_tokens_needed = message_tokens + requested_tokens
            
            logger.debug(
                "Token validation for chat completion",
                message_tokens=message_tokens,
                requested_tokens=requested_tokens,
                total_tokens=total_tokens_needed,
                max_allowed=max_tokens_allowed,
            )
            
            if total_tokens_needed > max_tokens_allowed:
                return False, (
                    f"Request exceeds maximum token limit. "
                    f"Messages: {message_tokens} tokens + "
                    f"Requested output: {requested_tokens} tokens = "
                    f"{total_tokens_needed} tokens. "
                    f"Maximum allowed: {max_tokens_allowed} tokens."
                )
            
        elif isinstance(request, OpenAICompletionRequest):
            prompt_tokens = _length_function(request.prompt)
            requested_tokens = request.max_tokens or 512
            total_tokens_needed = prompt_tokens + requested_tokens
            
            logger.debug(
                "Token validation for completion",
                prompt_tokens=prompt_tokens,
                requested_tokens=requested_tokens,
                total_tokens=total_tokens_needed,
                max_allowed=max_tokens_allowed,
            )
            
            if total_tokens_needed > max_tokens_allowed:
                return False, (
                    f"Request exceeds maximum token limit. "
                    f"Prompt: {prompt_tokens} tokens + "
                    f"Requested output: {requested_tokens} tokens = "
                    f"{total_tokens_needed} tokens. "
                    f"Maximum allowed: {max_tokens_allowed} tokens."
                )
        
        return True, ""
        
    except Exception as e:
        logger.warning("Error during token validation", error=str(e))
        return True, ""


@router.post(
    "/chat/completions",
    summary="OpenAI compatible chat completion endpoint using RAG",
    description="""Generate chat completions with Retrieval-Augmented Generation (RAG).

**Model Selection:**
- `openrag-{partition_name}`: Query only the specified partition
- `openrag-all`: Query across all available partitions
- empty or model name: Use the LLM directly

**Request Format:**
Accepts OpenAI-compatible chat completion requests with:
- `messages`: Array of chat messages (last must be from user)
- `model`: Model/partition to use
- `stream`: Optional streaming response (true/false)
- Standard OpenAI parameters (temperature, max_tokens, etc.)

**RAG Process:**
1. Extracts query from conversation
2. Retrieves relevant documents from specified partition(s)
3. Enriches prompt with document context
4. Generates completion using LLM

**Response:**
Returns OpenAI-compatible response with additional `extra` field containing:
- `sources`: Array of source documents with metadata and URLs

**Streaming:**
Set `stream: true` for Server-Sent Events (SSE) streaming responses.
""",
)
async def openai_chat_completion(
    request2: Request,
    request: OpenAIChatCompletionRequest = Body(...),
    _: None = Depends(check_llm_model_availability),
    user=Depends(current_user),
    user_partitions=Depends(current_user_or_admin_partitions_list),
):
    model_name = request.model or config.llm.get("model")
    log = logger.bind(model=model_name, endpoint="/chat/completions")

    if not request.messages or request.messages[-1].role != "user" or not request.messages[-1].content:
        log.warning("Invalid request: missing or malformed user message.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The last message must be a non-empty user message",
        )

    target_model_id = model_name if is_direct_llm_model(request) else config.llm.get("model")
    max_tokens_allowed = await get_max_model_tokens(target_model_id)
    is_valid, error_message = validate_tokens_limit(request, max_tokens_allowed=max_tokens_allowed)
    if not is_valid:
        log.warning("Token limit validation failed", detail=error_message)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=error_message,
        )

    log.debug(
        "Received chat completion request with messages: {}",
        truncate(str(request.messages)),
    )

    try:
        if is_direct_llm_model(request):
            partitions = None
        else:
            partitions = await get_partition_name(model_name, user_partitions, is_admin=user["is_admin"])
            log.debug(f"Using partitions: {partitions}")
    except Exception as e:
        log.warning("Invalid model or partition", error=str(e))
        raise

    try:
        llm_output, docs = await ragpipe.chat_completion(partition=partitions, payload=request.model_dump())
        log.debug("RAG chat completion pipeline executed.")
    except Exception as e:
        log.exception("Chat completion failed.", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Chat completion failed: {e!s}",
        )

    metadata = __prepare_sources(request2, docs)
    metadata_json = json.dumps({"sources": metadata})

    if request.stream:

        async def stream_response():
            try:
                async for line in llm_output:
                    if line.startswith("data:"):
                        if "[DONE]" in line:
                            yield f"{line}\n\n"
                        else:
                            try:
                                data_str = line[len("data: ") :]
                                data = json.loads(data_str)
                                data["model"] = model_name
                                data["extra"] = metadata_json
                                yield f"data: {json.dumps(data)}\n\n"
                            except json.JSONDecodeError as e:
                                log.error("Failed to decode streamed chunk.", error=str(e))
                                raise
            except Exception as e:
                log.warning("Error while generating streaming answer", error=str(e))
                error_chunk = {
                    "error": {
                        "message": f"Error while generating answer: {str(e)}",
                        "type": "error",
                        "param": None,
                        "code": "ERROR_ANSWER_GENERATION",
                    }
                }
                yield f"data: {json.dumps(error_chunk)}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(stream_response(), media_type="text/event-stream")
    else:
        try:
            chunk = await llm_output.__anext__()
            chunk["model"] = model_name
            chunk["extra"] = metadata_json
            log.debug("Returning non-streaming completion chunk.")
            return JSONResponse(content=chunk)
        except Exception as e:
            log.warning("Error while generating answer", error=str(e))
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error while generating answer: {e!s}",
            )


@router.post(
    "/completions",
    summary="OpenAI compatible completion endpoint using RAG",
    description="""Generate text completions with Retrieval-Augmented Generation (RAG).

**Model Selection:**
- `openrag-{partition_name}`: Query only the specified partition
- `openrag-all`: Query across all available partitions
- empty or model name: Use the LLM directly

**Request Format:**
Accepts OpenAI-compatible completion requests with:
- `prompt`: Text prompt for completion
- `model`: Model/partition to use
- Standard OpenAI parameters (temperature, max_tokens, etc.)

**RAG Process:**
1. Retrieves relevant documents from specified partition(s)
2. Enriches prompt with document context
3. Generates completion using LLM

**Response:**
Returns OpenAI-compatible response with additional `extra` field containing:
- `sources`: Array of source documents with metadata and URLs

**Note:** Streaming is not supported for this endpoint.
""",
)
async def openai_completion(
    request2: Request,
    request: OpenAICompletionRequest,
    _: None = Depends(check_llm_model_availability),
    user=Depends(current_user),
    user_partitions=Depends(current_user_or_admin_partitions_list),
):
    model_name = request.model or config.llm.get("model")
    log = logger.bind(model=model_name, endpoint="/completions")

    if not request.prompt:
        log.warning("Prompt is missing.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The prompt is required",
        )

    if request.stream:
        log.warning("Streaming not supported for this endpoint.")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Streaming is not supported for this endpoint",
        )

    target_model_id = model_name if is_direct_llm_model(request) else config.llm.get("model")
    max_tokens_allowed = await get_max_model_tokens(target_model_id)
    is_valid, error_message = validate_tokens_limit(request, max_tokens_allowed=max_tokens_allowed)
    if not is_valid:
        log.warning("Token limit validation failed", detail=error_message)
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=error_message,
        )

    try:
        if is_direct_llm_model(request):
            partitions = None
        else:
            partitions = await get_partition_name(model_name, user_partitions, is_admin=user["is_admin"])

    except Exception as e:
        log.warning(f"Invalid model or partition: {e}")
        raise

    try:
        llm_output, docs = await ragpipe.completions(partition=partitions, payload=request.model_dump())
        log.debug("RAG completion pipeline executed.")
    except Exception as e:
        log.exception("Completion request failed.", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Completion failed: {e!s}",
        )

    metadata = __prepare_sources(request2, docs)
    metadata_json = json.dumps({"sources": metadata})

    try:
        complete_response = await llm_output.__anext__()
        complete_response["extra"] = metadata_json
        log.debug("Returning completion response.")
        return JSONResponse(content=complete_response)
    except Exception as e:
        log.warning("No response from LLM.", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"No response from LLM: {e!s}",
        )
