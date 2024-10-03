# Requirement:
#   pip install openai
# Usage:
#   python openai_api.py
# Visit http://localhost:8000/docs for documents.

import base64
import copy
import json
import time
from argparse import ArgumentParser
from contextlib import asynccontextmanager
from threading import Thread
from typing import Dict, List, Literal, Optional, Union, Any

import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from loguru import logger
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from transformers import AutoModelForCausalLM, AutoTokenizer
from transformers import GenerationConfig, TextIteratorStreamer

from template import get_conv_template


class BasicAuthMiddleware(BaseHTTPMiddleware):

    def __init__(self, app, username: str, password: str):
        super().__init__(app)
        self.required_credentials = base64.b64encode(
            f'{username}:{password}'.encode()).decode()

    async def dispatch(self, request: Request, call_next):
        authorization: str = request.headers.get('Authorization')
        if authorization:
            try:
                schema, credentials = authorization.split()
                if credentials == self.required_credentials:
                    return await call_next(request)
            except ValueError:
                pass

        headers = {'WWW-Authenticate': 'Basic'}
        return Response(status_code=401, headers=headers)


def _gc(forced: bool = False):
    global args
    if args.disable_gc and not forced:
        return

    import gc

    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


@asynccontextmanager
async def lifespan(app: FastAPI):  # collects GPU memory
    yield
    _gc(forced=True)


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=['*'],
    allow_credentials=True,
    allow_methods=['*'],
    allow_headers=['*'],
)


class ModelCard(BaseModel):
    id: str
    object: str = 'model'
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = 'owner'
    root: Optional[str] = None
    parent: Optional[str] = None
    permission: Optional[list] = None


class ModelList(BaseModel):
    object: str = 'list'
    data: List[ModelCard] = []


class ChatMessage(BaseModel):
    role: Literal['user', 'assistant', 'system', 'function', 'tool']
    content: Optional[str] = None
    tool_calls: Optional[Dict] = None


class DeltaMessage(BaseModel):
    role: Optional[Literal['user', 'assistant', 'system']] = None
    content: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    tools: Optional[List[Dict]] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    max_length: Optional[int] = None
    stream: Optional[bool] = False
    stop: Optional[List[str]] = None


class ChatCompletionResponseChoice(BaseModel):
    index: int
    message: Union[ChatMessage]
    finish_reason: Literal['stop', 'length', 'tool_calls']


class ChatCompletionResponseStreamChoice(BaseModel):
    index: int
    delta: DeltaMessage
    finish_reason: Optional[Literal['stop', 'length']] = None


class ChatCompletionResponseUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    id: Literal["chatcmpl-default"] = "chatcmpl-default"
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatCompletionResponseChoice]
    usage: ChatCompletionResponseUsage


class ChatCompletionStreamResponse(BaseModel):
    id: Literal["chatcmpl-default"] = "chatcmpl-default"
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatCompletionResponseStreamChoice]


@app.get('/v1/models', response_model=ModelList)
async def list_models():
    global model_args
    model_card = ModelCard(id='gpt-3.5-turbo')
    return ModelList(data=[model_card])


# To work around that unpleasant leading-\n tokenization issue!
def add_extra_stop_words(stop_words):
    _stop_words = []
    if stop_words:
        _stop_words.extend(stop_words)
        for x in stop_words:
            s = x.lstrip('\n')
            if s and (s not in _stop_words):
                _stop_words.append(s)
    return _stop_words


def trim_stop_words(response, stop_words):
    if stop_words:
        for stop in stop_words:
            idx = response.find(stop)
            if idx != -1:
                response = response[:idx]
    return response


TOOL_DESC = (
    '{name_for_model}: Call this tool to interact with the {name_for_human} API.'
    ' What is the {name_for_human} API useful for? {description_for_model} Parameters: {parameters}'
)

REACT_INSTRUCTION = """Answer the following questions as best you can. You have access to the following APIs:

{tools_text}

Use the following format:

Question: the input question you must answer
Thought: you should always think about what to do
Action: the action to take, should be one of [{tools_name_text}]
Action Input: the input to the action
Observation: the result of the action
... (this Thought/Action/Action Input/Observation can be repeated zero or more times)
Thought: I now know the final answer
Final Answer: the final answer to the original input question

Begin!"""

_TEXT_COMPLETION_CMD = object()


def parse_messages(messages, tools):
    if all(m.role != 'user' for m in messages):
        raise HTTPException(
            status_code=400,
            detail='Invalid request: Expecting at least one user message.',
        )

    messages = copy.deepcopy(messages)
    if messages[0].role == 'system':
        system = messages.pop(0).content.lstrip('\n').rstrip()
    else:
        system = ''

    if tools:
        tools_text = []
        tools_name_text = []
        for tool_info in tools:
            name = tool_info.get('name', '')
            name_m = tool_info.get('name_for_model', name)
            name_h = tool_info.get('name_for_human', name)
            desc = tool_info.get('description', '')
            desc_m = tool_info.get('description_for_model', desc)
            params = tool_info.get('parameters', {})
            tool = TOOL_DESC.format(
                name_for_model=name_m,
                name_for_human=name_h,
                # Hint: You can add the following format requirements in description:
                #   "Format the arguments as a JSON object."
                #   "Enclose the code within triple backticks (`) at the beginning and end of the code."
                description_for_model=desc_m,
                parameters=json.dumps(params, ensure_ascii=False),
            )
            tools_text.append(tool)
            tools_name_text.append(name_m)
        tools_text = '\n\n'.join(tools_text)
        tools_name_text = ', '.join(tools_name_text)
        instruction = (REACT_INSTRUCTION.format(
            tools_text=tools_text,
            tools_name_text=tools_name_text,
        ).lstrip('\n').rstrip())
    else:
        instruction = ''

    messages_with_fncall = messages
    messages = []
    for m_idx, m in enumerate(messages_with_fncall):
        role, content, tool_calls = m.role, m.content, m.tool_calls
        content = content or ''
        content = content.lstrip('\n').rstrip()
        if role == 'function':
            if (len(messages) == 0) or (messages[-1].role != 'assistant'):
                raise HTTPException(
                    status_code=400,
                    detail='Invalid request: Expecting role assistant before role function.',
                )
            messages[-1].content += f'\nObservation: {content}'
            if m_idx == len(messages_with_fncall) - 1:
                # add a prefix for text completion
                messages[-1].content += '\nThought:'
        elif role == 'assistant':
            if len(messages) == 0:
                raise HTTPException(
                    status_code=400,
                    detail=
                    'Invalid request: Expecting role user before role assistant.',
                )
            if tool_calls is None:
                if tools:
                    content = f'Thought: I now know the final answer.\nFinal Answer: {content}'
            else:
                f_name, f_args = tool_calls['name'], tool_calls['arguments']
                if not content.startswith('Thought:'):
                    content = f'Thought: {content}'
                content = f'{content}\nAction: {f_name}\nAction Input: {f_args}'
            if messages[-1].role == 'user':
                messages.append(
                    ChatMessage(role='assistant',
                                content=content.lstrip('\n').rstrip()))
            else:
                messages[-1].content += '\n' + content
        elif role == 'user':
            messages.append(
                ChatMessage(role='user',
                            content=content.lstrip('\n').rstrip()))
        else:
            raise HTTPException(
                status_code=400,
                detail=f'Invalid request: Incorrect role {role}.')

    query = _TEXT_COMPLETION_CMD
    if messages[-1].role == 'user':
        query = messages[-1].content
        messages = messages[:-1]

    if len(messages) % 2 != 0:
        raise HTTPException(status_code=400, detail='Invalid request')

    history = []  # [(Q1, A1), (Q2, A2), ..., (Q_last_turn, A_last_turn)]
    for i in range(0, len(messages), 2):
        if messages[i].role == 'user' and messages[i + 1].role == 'assistant':
            usr_msg = messages[i].content.lstrip('\n').rstrip()
            bot_msg = messages[i + 1].content.lstrip('\n').rstrip()
            if instruction and (i == len(messages) - 2):
                usr_msg = f'{instruction}\n\nQuestion: {usr_msg}'
                instruction = ''
            history.append([usr_msg, bot_msg])
        else:
            raise HTTPException(
                status_code=400,
                detail='Invalid request: Expecting exactly one user (or function) role before every assistant role.',
            )
    if instruction:
        assert query is not _TEXT_COMPLETION_CMD
        query = f'{instruction}\n\nQuestion: {query}'
    return query, history, system


def parse_response(response):
    func_name, func_args = '', ''
    i = response.find('\nAction:')
    j = response.find('\nAction Input:')
    k = response.find('\nObservation:')
    if 0 <= i < j:  # If the text has `Action` and `Action input`,
        if k < j:  # but does not contain `Observation`,
            # then it is likely that `Observation` is omitted by the LLM,
            # because the output text may have discarded the stop word.
            response = response.rstrip() + '\nObservation:'  # Add it back.
        k = response.find('\nObservation:')
        func_name = response[i + len('\nAction:'):j].strip()
        func_args = response[j + len('\nAction Input:'):k].strip()

    if func_name:
        response = response[:i]
        t = response.find('Thought: ')
        if t >= 0:
            response = response[t + len('Thought: '):]
        response = response.strip()
        choice_data = ChatCompletionResponseChoice(
            index=0,
            message=ChatMessage(
                role='assistant',
                content=response,
                tool_calls={
                    'name': func_name,
                    'arguments': func_args
                },
            ),
            finish_reason='tool_calls',
        )
        return choice_data

    z = response.rfind('\nFinal Answer: ')
    if z >= 0:
        response = response[z + len('\nFinal Answer: '):]
    choice_data = ChatCompletionResponseChoice(
        index=0,
        message=ChatMessage(role='assistant', content=response),
        finish_reason='stop',
    )
    return choice_data


def prepare_chat(tokenizer, query, history, system):
    """Prepare model inputs for chat completion."""
    if prompt_template:
        history_messages = history + [[query, ""]]
        prompt = prompt_template.get_prompt(messages=history_messages, system_prompt=system)
    else:
        messages = [
            {"role": "system", "content": system}
        ]
        for i, (question, response) in enumerate(history):
            question = question.lstrip('\n').rstrip()
            response = response.lstrip('\n').rstrip()
            messages.append({"role": "user", "content": question})
            messages.append({"role": "assistant", "content": response})
        messages.append({"role": "user", "content": query})
        prompt = tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True
        )
    model_inputs = tokenizer([prompt], return_tensors='pt')
    return model_inputs


def model_chat(model, tokenizer, query, history, gen_kwargs, system):
    """Generate chat completion from the model."""
    model_inputs = prepare_chat(tokenizer, query, history, system).to(model.device)
    generated_ids = model.generate(model_inputs.input_ids, **gen_kwargs)
    generated_ids = [
        output_ids[len(input_ids):] for input_ids, output_ids in zip(model_inputs.input_ids, generated_ids)
    ]
    response = tokenizer.batch_decode(generated_ids, skip_special_tokens=True)[0]
    prompt_length = len(model_inputs.input_ids[0])
    response_length = len(generated_ids[0])
    return response, prompt_length, response_length


def stream_model_chat(model, tokenizer, query, history, gen_kwargs, system):
    """Generate chat completion from the model in stream mode."""
    model_inputs = prepare_chat(tokenizer, query, history, system).to(model.device)
    gen_kwargs['inputs'] = model_inputs.input_ids

    streamer = TextIteratorStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
    gen_kwargs['streamer'] = streamer
    thread = Thread(target=model.generate, kwargs=gen_kwargs, daemon=True)
    thread.start()

    yield from streamer


@app.post('/v1/chat/completions', response_model=ChatCompletionResponse)
async def create_chat_completion(request: ChatCompletionRequest):
    """Generate chat completion."""
    global model, tokenizer

    gen_kwargs = {}
    if request.top_k is not None:
        gen_kwargs['top_k'] = request.top_k
    if request.temperature is not None:
        if request.temperature < 0.01:
            gen_kwargs['top_k'] = 1  # greedy decoding
        else:
            # Not recommended. Please tune top_p instead.
            gen_kwargs['temperature'] = request.temperature
    if request.top_p is not None:
        gen_kwargs['top_p'] = request.top_p
    if request.max_length is not None:
        gen_kwargs['max_length'] = request.max_length

    stop_words = add_extra_stop_words(request.stop)
    if request.tools:
        stop_words = stop_words or []
        if 'Observation:' not in stop_words:
            stop_words.append('Observation:')

    query, history, system = parse_messages(request.messages, request.tools)

    if request.stream:
        if request.tools:
            raise HTTPException(
                status_code=400,
                detail='Invalid request: Function calling is not yet implemented for stream mode.',
            )
        generate = stream_chat_completion(
            query,
            history,
            request.model,
            stop_words,
            gen_kwargs,
            system=system
        )
        return StreamingResponse(generate, media_type='text/event-stream')

    response, prompt_length, response_length = model_chat(
        model,
        tokenizer,
        query,
        history,
        gen_kwargs=gen_kwargs,
        system=system
    )
    logger.debug(f'*** history begin ***\n{history}\n*** history end ***\n'
                 f'question: {query}\nresponse: {response}\n')
    _gc()

    response = trim_stop_words(response, stop_words)
    if request.tools:
        choice_data = parse_response(response)
    else:
        choice_data = ChatCompletionResponseChoice(
            index=0,
            message=ChatMessage(role='assistant', content=response),
            finish_reason='stop',
        )

    usage = ChatCompletionResponseUsage(
        prompt_tokens=prompt_length,
        completion_tokens=response_length,
        total_tokens=prompt_length + response_length,
    )
    return ChatCompletionResponse(model=request.model, choices=[choice_data], usage=usage)


def dictify(data: BaseModel) -> Dict[str, Any]:
    try:  # pydantic v2
        return data.model_dump(exclude_unset=True)
    except AttributeError:  # pydantic v1
        return data.dict(exclude_unset=True)


def jsonify(data: BaseModel) -> str:
    try:  # pydantic v2
        return json.dumps(data.model_dump(exclude_unset=True), ensure_ascii=False)
    except AttributeError:  # pydantic v1
        return data.json(exclude_unset=True, ensure_ascii=False)


async def stream_chat_completion(
        query: str,
        history: List[List[str]],
        model_id: str,
        stop_words: List[str],
        gen_kwargs: Dict,
        system: str,
):
    """Generate chat completion in stream mode."""
    global model, tokenizer
    choice_data = ChatCompletionResponseStreamChoice(
        index=0, delta=DeltaMessage(role='assistant', content=""), finish_reason=None)
    chunk = ChatCompletionStreamResponse(model=model_id, choices=[choice_data])
    yield jsonify(chunk)

    stop_words = [x for x in stop_words if x]
    response_generator = stream_model_chat(
        model,
        tokenizer,
        query,
        history,
        gen_kwargs,
        system
    )
    for token_output in response_generator:
        # Check if any stop word is in the token output
        if any(stop_word in token_output for stop_word in stop_words):
            break

        # Send the current token as part of the response
        choice_data = ChatCompletionResponseStreamChoice(
            index=0, delta=DeltaMessage(content=token_output), finish_reason=None)
        chunk = ChatCompletionStreamResponse(model=model_id, choices=[choice_data])
        yield jsonify(chunk)

    choice_data = ChatCompletionResponseStreamChoice(
        index=0, delta=DeltaMessage(), finish_reason='stop'
    )
    chunk = ChatCompletionStreamResponse(model=model_id, choices=[choice_data])
    yield jsonify(chunk)
    yield '[DONE]'

    _gc()


if __name__ == '__main__':
    parser = ArgumentParser()
    parser.add_argument('--base_model', type=str, default='Qwen/Qwen-7B-Chat', help='Model name or path')
    parser.add_argument('--lora_model', default=None, type=str, help="If None, perform inference on the base model")
    parser.add_argument('--template_name', default=None, type=str,
                        help="Prompt template name, eg: alpaca, vicuna, baichuan, chatglm2 etc.")
    parser.add_argument('--api_auth', help='API authentication credentials')
    parser.add_argument('--cpu_only', action='store_true', help='Run demo with CPU only')
    parser.add_argument('--server_port', type=int, default=8000, help='Demo server port.')
    parser.add_argument('--server_name', type=str, default='127.0.0.1',
                        help=('Demo server name. Default: 127.0.0.1, which is only visible from the local computer. '
                              'If you want other computers to access your server, use 0.0.0.0 instead.')
                        )
    parser.add_argument('--disable_gc', action='store_true', help='Disable GC after each response generated.')

    args = parser.parse_args()
    logger.info(args)

    if args.api_auth:
        app.add_middleware(
            BasicAuthMiddleware,
            username=args.api_auth.split(':')[0],
            password=args.api_auth.split(':')[1]
        )

    tokenizer = AutoTokenizer.from_pretrained(
        args.base_model,
        trust_remote_code=True,
        resume_download=True,
    )

    if args.cpu_only:
        device_map = 'cpu'
    else:
        device_map = 'auto'
    model = AutoModelForCausalLM.from_pretrained(
        args.base_model,
        device_map=device_map,
        trust_remote_code=True,
        resume_download=True,
    )
    if args.lora_model:
        from peft import PeftModel

        model = PeftModel.from_pretrained(model, args.lora_model, device_map=device_map)
        logger.debug(f'Loaded LORA model: {args.lora_model}')

    model = model.eval()
    model.generation_config = GenerationConfig.from_pretrained(
        args.base_model,
        trust_remote_code=True,
        resume_download=True,
    )
    if args.template_name:
        prompt_template = get_conv_template(args.template_name)
    else:
        prompt_template = None

    uvicorn.run(app, host=args.server_name, port=args.server_port, workers=1)
