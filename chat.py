import operator
from typing import *
import requests
import tempfile
import os

# LangChain
import langchain
from langchain.tools import BaseTool
from langchain_core.messages import AnyMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langchain_aws import ChatBedrock

# LangGraph
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
from langchain_community.document_loaders import UnstructuredPDFLoader
from langchain.tools import Tool

# Tilores
from tilores import TiloresAPI
from tilores_langchain import TiloresTools

# Chainlit
import chainlit as cl
from chainlit.sync import run_sync

# Plotly
import plotly.graph_objects as go
from plotly.io import from_json


class HumanInputChainlit(BaseTool):
    """Tool that adds the capability to ask user for input."""

    name: str = "human"
    description: str = (
        "You can ask a human for guidance when you think you "
        "got stuck or you are not sure what to do next. "
        "The input should be a question for the human."
    )

    def _run(self, query: str, run_manager=None) -> str:
        """Use the Human input tool."""

        res = run_sync(cl.AskUserMessage(content=query).send())
        return res["content"]

    async def _arun(self, query: str, run_manager=None) -> str:
        """Use the Human input tool."""
        res = await cl.AskUserMessage(content=query).send()
        return res["output"]


class ChatState(TypedDict):
    messages: Annotated[Sequence[AnyMessage], operator.add]

@cl.on_chat_start
def start():
    if os.environ.get("LLM_PROVIDER") == "Bedrock":
        llm = ChatBedrock(
            credentials_profile_name=os.environ["BEDROCK_CREDENTIALS_PROFILE_NAME"],
            region_name=os.environ["BEDROCK_REGION"],
            model_id=os.environ["BEDROCK_MODEL_ID"],
            streaming=True,
            model_kwargs={"temperature": 0},
        )
    else:
        model_name = "gpt-4o-mini"
        if os.environ.get("OPENAI_MODEL_NAME"):
            model_name = os.environ.get("OPENAI_MODEL_NAME")
        llm = ChatOpenAI(temperature=0, streaming=True, model_name=model_name)

    # Setup a connection to the Tilores instance and provide it as a tool
    tilores = TiloresAPI.from_environ()
    tilores_tools = TiloresTools(tilores)
    tools = [
        HumanInputChainlit(),
        tilores_tools.search_tool(),
        tilores_tools.edge_tool(),
        pdf_tool,
        plotly_tool,
    ]
    # Use MemorySaver to use the full conversation
    memory = MemorySaver()
    state = ChatState(messages=[])
    # Use a LangGraph agent
    agent = create_react_agent(llm, tools, checkpointer=memory)

    # Provide the runnable and state to the user session
    cl.user_session.set("runnable", agent)
    cl.user_session.set("state", state)

@cl.on_message
async def main(message: cl.Message):
    # Retrieve the runnable and state from the user session
    runnable = cl.user_session.get("runnable")
    state = cl.user_session.get("state")

    # Append the new message to the state
    state['messages'] += [HumanMessage(content=message.content)]

    # Stream the response to the UI
    ui_message = cl.Message(content="")
    await ui_message.send()
    async for event in runnable.astream_events(state, version="v1", config={'configurable': {'thread_id': 'thread-1'}}):
        if event["event"] == "on_tool_end":
            if event["data"].get('output') and event["data"].get('output').artifact:
                fig = from_json(event["data"].get("output").artifact)
                chart = cl.Plotly(name="chart", figure=fig, display="inline")
                ui_message.elements.append(chart)

        if event["event"] == "on_chat_model_stream":
            c = event["data"]["chunk"].content
            if c and len(c) > 0 and isinstance(c[0], dict) and c[0]["type"] == "text":
                content = c[0]["text"]
            elif isinstance(c, str):
                content = c
            else:
                content = ""
            await ui_message.stream_token(token=content)

    await ui_message.update()

def load_pdf_from_url(url: str):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'
    }
    response = requests.get(url, headers=headers)
    if response.status_code == 200:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as temp_pdf:
            temp_pdf.write(response.content)

        loader = UnstructuredPDFLoader(temp_pdf.name)
        documents = loader.load()
        return documents
    else:
        raise Exception(f"Failed to download PDF from {url}. Status code: {response.status_code}")

pdf_tool = Tool(
    name = "load_pdf",
    func=load_pdf_from_url,
    description="useful for when you need to download and process a PDF file from a given URL"
)

def render_plotly_graph(figureCode: str):
    local_vars = {}
    exec(figureCode, {"go": go}, local_vars)
    fig = local_vars.get("fig")
    return "generated a chart from the provided figure", fig.to_json()

plotly_tool = Tool(
    name = "plotly_tool",
    func=render_plotly_graph,
    description="useful for when you need to render a graph using plotly; the figureCode must only import plotly.graph_objects as go and must provide a local variable named fig as a result",
    response_format='content_and_artifact'
)