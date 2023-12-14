# system lib
import os
import json
import time
from contextlib import asynccontextmanager
from typing import List, Literal, Optional, Union

# 3rd lib
import torch
import uvicorn
from text2vec import SentenceModel
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse
from transformers import AutoTokenizer

# my util
from utils import process_response, generate_chatglm3, generate_stream_chatglm3, load_model_on_gpus

MODEL = 'chatglm3-6b-32k'


@asynccontextmanager
async def lifespan(app: FastAPI):  # collects GPU memory
    yield
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.ipc_collect()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ModelCard(BaseModel):
    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "owner"
    root: Optional[str] = None
    parent: Optional[str] = None
    permission: Optional[list] = None


class ModelList(BaseModel):
    object: str = "list"
    data: List[ModelCard] = []


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system", "observation"]
    content: str = None
    metadata: Optional[str] = None
    tools: Optional[List[dict]] = None


class DeltaMessage(BaseModel):
    role: Optional[Literal["user", "assistant", "system"]] = None
    content: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    messages: List[ChatMessage]
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 1.0
    max_tokens: Optional[int] = 4096
    stop: Optional[Union[str, List[str]]] = None
    stream: Optional[bool] = False
    chunk: Optional[bool] = True

    # Additional parameters support for stop generation
    stop_token_ids: Optional[List[int]] = None
    repetition_penalty: Optional[float] = 1.1

    # Additional parameters supported by tools
    return_function_call: Optional[bool] = False


class ChatCompletionResponseChoice(BaseModel):
    index: int
    message: ChatMessage
    finish_reason: Literal["stop", "length", "function_call"]
    history: Optional[List[dict]] = None


class ChatCompletionResponseStreamChoice(BaseModel):
    index: int
    delta: DeltaMessage
    finish_reason: Optional[Literal["stop", "length"]]


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    total_tokens: int = 0
    completion_tokens: Optional[int] = 0


class ChatCompletionResponse(BaseModel):
    model: str
    object: Literal["chat.completion", "chat.completion.chunk"]
    choices: List[Union[ChatCompletionResponseChoice,
                        ChatCompletionResponseStreamChoice]]
    created: Optional[int] = Field(default_factory=lambda: int(time.time()))
    usage: Optional[UsageInfo] = None


class EmbeddingRequest(BaseModel):
    model: str = 'text2vec-large-chinese'
    prompt: List[str]


class EmbeddingResponse(BaseModel):
    data: List[List[float]]
    model: str
    object: str


class TokenizeRequest(BaseModel):
    prompt: str
    max_tokens: int = 4096


class TokenizeResponse(BaseModel):
    tokenIds: List[int]
    tokens: List[str]
    model: str
    object: str


@app.get("/models", response_model=ModelList)
async def list_models():
    global model, tokenizer
    models = [
        ModelCard(id="text2vec-large-chinese", object="embedding"),
        ModelCard(id="text2vec-base-chinese-paraphrase", object="embedding")
    ]

    if model is not None and tokenizer is not None:
        models.append(ModelCard(id="chatglm3-6b-32k",
                      object="chat.completion"))

    return ModelList(data=models)


@app.post("/chat", response_model=ChatCompletionResponse)
async def create_chat_completion(request: ChatCompletionRequest):
    global model, tokenizer

    if model is None or tokenizer is None:
        raise HTTPException(status_code=404, detail="chat API not available")

    if request.messages[-1].role == "assistant":
        raise HTTPException(status_code=400, detail="Invalid request")

    with_function_call = bool(
        request.messages[0].role == "system" and request.messages[0].tools is not None)

    # stop settings
    request.stop = request.stop or []
    if isinstance(request.stop, str):
        request.stop = [request.stop]

    request.stop_token_ids = request.stop_token_ids or []

    gen_params = dict(
        messages=request.messages,
        temperature=request.temperature,
        top_p=request.top_p,
        max_tokens=request.max_tokens or 1024,
        echo=False,
        stream=request.stream,
        chunk=request.chunk,
        stop_token_ids=request.stop_token_ids,
        stop=request.stop,
        repetition_penalty=request.repetition_penalty,
        with_function_call=with_function_call,
    )

    if request.stream:
        generate = predict(MODEL, gen_params)
        return EventSourceResponse(generate, media_type="text/event-stream")

    response = generate_chatglm3(model, tokenizer, gen_params)
    usage = UsageInfo()

    finish_reason, history = "stop", None
    if with_function_call and request.return_function_call:
        history = [m.dict(exclude_none=True) for m in request.messages]
        content, history = process_response(response["text"], history)
        if isinstance(content, dict):
            message, finish_reason = ChatMessage(
                role="assistant",
                content=json.dumps(content, ensure_ascii=False),
            ), "function_call"
        else:
            message = ChatMessage(role="assistant", content=content)
    else:
        message = ChatMessage(role="assistant", content=response["text"])

    choice_data = ChatCompletionResponseChoice(
        index=0,
        message=message,
        finish_reason=finish_reason,
        history=history
    )

    task_usage = UsageInfo.parse_obj(response["usage"])
    for usage_key, usage_value in task_usage.dict().items():
        setattr(usage, usage_key, getattr(usage, usage_key) + usage_value)

    return ChatCompletionResponse(model=MODEL, choices=[choice_data], object="chat.completion", usage=usage)


@app.post('/embedding', response_model=EmbeddingResponse)
async def embedding(request: EmbeddingRequest):
    global encoder

    embeddings = encoder[request.model].encode(request.prompt)
    data = embeddings.tolist()
    return EmbeddingResponse(data=data, model=request.model, object='embedding')


@app.post('/tokenize', response_model=TokenizeResponse)
async def tokenize(request: TokenizeRequest):
    global tokenizer

    if tokenizer is None:
        raise HTTPException(
            status_code=404, detail="API tokenize not available")

    tokens = tokenizer.tokenize(request.prompt)
    tokenIds = tokenizer(request.prompt, truncation=True,
                         max_length=request.max_tokens)['input_ids']
    return TokenizeResponse(tokenIds=tokenIds, tokens=tokens, model=MODEL, object="tokenizer")


async def predict(model_id: str, params: dict):
    global model, tokenizer

    if model is None or tokenizer is None:
        raise HTTPException(
            status_code=404, detail="model and tokenizer not available")

    choice_data = ChatCompletionResponseStreamChoice(
        index=0,
        delta=DeltaMessage(role="assistant"),
        finish_reason=None
    )
    chunk = ChatCompletionResponse(model=model_id, choices=[
                                   choice_data], object="chat.completion.chunk")
    yield "{}".format(chunk.json(exclude_unset=True, ensure_ascii=False))

    previous_text = ""
    for new_response in generate_stream_chatglm3(model, tokenizer, params):
        decoded_unicode = new_response["text"]
        if params["chunk"]:
            delta_text = decoded_unicode[len(previous_text):]
            previous_text = decoded_unicode
        else:
            delta_text = decoded_unicode

        if (len(delta_text)):
            choice_data = ChatCompletionResponseStreamChoice(
                index=0,
                delta=DeltaMessage(content=delta_text),
                finish_reason=None
            )
            chunk = ChatCompletionResponse(model=model_id, choices=[
                choice_data], object="chat.completion.chunk")
            yield "{}".format(chunk.json(exclude_unset=True, ensure_ascii=False))

    choice_data = ChatCompletionResponseStreamChoice(
        index=0,
        delta=DeltaMessage(),
        finish_reason="stop"
    )
    chunk = ChatCompletionResponse(model=model_id, choices=[
                                   choice_data], object="chat.completion.chunk")
    yield "{}".format(chunk.json(exclude_unset=True, ensure_ascii=False))
    yield '[DONE]'


def list_cuda():
    # count all cuda
    cuda_devices = [torch.device(f'cuda:{i}')
                    for i in range(torch.cuda.device_count())]

    # print each cuda info
    for device in cuda_devices:
        device_name = torch.cuda.get_device_name(device)
        device_index = device.index
        is_available = torch.cuda.is_available()
        print(
            f"Device name: {device_name}, Device index: {device_index}, Is available: {is_available}")


def list_cuda():
    # Check if CUDA is available before proceeding
    if not torch.cuda.is_available():
        print("CUDA is not available.")
        return

    for i in range(torch.cuda.device_count()):
        device_name = torch.cuda.get_device_name(i)
        print(
            f"Device name: {device_name}, Device index: {i}, Is available: True")


if __name__ == "__main__":

    available_gpus = torch.cuda.device_count()

    tokenizer = AutoTokenizer.from_pretrained(
        "THUDM/chatglm3-6b-32k", trust_remote_code=True)

    if available_gpus > 0:
        print('GPU mode')
        print("CUDA_VISIBLE_DEVICES", os.environ["CUDA_VISIBLE_DEVICES"])
        list_cuda()
        model = load_model_on_gpus(
            "THUDM/chatglm3-6b-32k", available_gpus)
    else:
        print('CPU mode, chat API not available')
        model = None

    encoder = {
        'text2vec-large-chinese': SentenceModel('GanymedeNil/text2vec-large-chinese', device='cpu'),
        'text2vec-base-chinese-paraphrase': SentenceModel('shibing624/text2vec-base-chinese-paraphrase', device='cpu')
    }

    uvicorn.run(app, host='0.0.0.0', port=8100)
