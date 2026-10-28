# syntax=docker/dockerfile:1
#
# Container image for the PharmaPulse streaming backend Lambda, built as a CDK
# DockerImageAsset and deployed as a container-image Lambda behind a Function
# URL with RESPONSE_STREAM invoke mode (see stacks/backend_stack.py).
#
# Using the AWS Lambda Python base image keeps the Runtime Interface Client
# built in, so the zip/bundling path (which needs Docker at synth time) is
# avoided and the asset is only built at synth/deploy. The handler validates
# the Cognito JWT and streams the agent response as SSE.
#
# ARM64 matches the CDK asset platform (see stacks/backend_stack.py).
FROM --platform=linux/arm64 public.ecr.aws/lambda/python:3.12

# Runtime deps the handler needs: python-jose for JWT signature verification
# (backend/jwt_validator.py) and boto3 for invoking the agent runtime (boto3
# ships in the Lambda image, but pin it here for local parity).
RUN pip install --no-cache-dir "python-jose>=3.3.0"

# Copy only the backend package (handler + jwt_validator) into the task root.
COPY backend/__init__.py ${LAMBDA_TASK_ROOT}/backend/__init__.py
COPY backend/jwt_validator.py ${LAMBDA_TASK_ROOT}/backend/jwt_validator.py
COPY backend/handler.py ${LAMBDA_TASK_ROOT}/backend/handler.py

# The streaming entrypoint is backend/handler.py :: handler.
CMD ["backend.handler.handler"]
