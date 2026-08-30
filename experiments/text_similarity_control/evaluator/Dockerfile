FROM python:3.12-slim
WORKDIR /benchmark
RUN pip install --no-cache-dir scipy
COPY evaluator.py pairs.json evaluate.sh inject.py ./
RUN chmod +x evaluate.sh
ENV DEAD_WEIGHT=0.0
ENTRYPOINT ["./evaluate.sh"]
