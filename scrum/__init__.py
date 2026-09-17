"""
The agentic scrum system.

Three entry points sit outside this package, because each is a different way of
starting the same machine:

    server.py           the office UI and its HTTP endpoints
    orchestrator.py     the command line
    lambda_handlers.py  AWS Lambda

Everything they drive lives in here:

    config      which backend won the probe, and why
    events      the event vocabulary and the routing table
    handlers    what each agent does — transport-agnostic
    runtime     the local adapter (a thread per queue)
    llm         chat model construction
    ui_state    the read model the office UI polls
    agents/     the ReAct loop, tools, reflection, and the three workers
    bus/        SNS+SQS, or in-process queues
    store/      S3+DynamoDB, or the workspace directory
"""
