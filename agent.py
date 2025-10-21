from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from langchain_openai import ChatOpenAI

token_provider = get_bearer_token_provider(
    DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
)

llm = ChatOpenAI(
  model = "gpt-5-mini",
  base_url = "https://{your_resource_name}.openai.azure.com/openai/v1/",
  api_key = token_provider
)


# Example invocation
messages = [
("system", "You are a helpful assistant."),
("human", "Translate 'I love programming' to French.")
]
response = llm.invoke(messages)
print(response.content)
