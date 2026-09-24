FROM ghcr.io/astral-sh/uv:0.12.18 AS uv
FROM public.ecr.aws/lambda/python:3.12

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR ${LAMBDA_TASK_ROOT}
COPY pyproject.toml uv.lock ./
RUN uv export --locked --only-group optimizer --no-hashes --no-header --no-annotate -o /tmp/optimizer-requirements.txt \
    && uv pip install --system --no-cache -r /tmp/optimizer-requirements.txt
COPY services/__init__.py ./services/__init__.py
COPY services/optimizer/__init__.py services/optimizer/solver.py services/optimizer/handler.py ./services/optimizer/
CMD ["services.optimizer.handler.lambda_handler"]
