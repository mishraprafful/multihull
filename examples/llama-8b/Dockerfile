FROM vllm/vllm-openai:v0.11.0

ENV VLLM_LOGGING_LEVEL=INFO \
    HF_HOME=/root/.cache/huggingface

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

ENTRYPOINT ["vllm", "serve"]
CMD ["meta-llama/Llama-3.1-8B-Instruct", "--host", "0.0.0.0", "--port", "8000"]
