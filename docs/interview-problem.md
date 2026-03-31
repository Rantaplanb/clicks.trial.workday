*Part 1:*

We have not yet implemented GPT-5.4 for our clicks agents. The goal for today is to build a simple, self-contained computer-use agent loop using GPT-5.4 so we can test the model and gather initial insights.

Ideally, by the end of the day we have:

•⁠  ⁠A Python agent loop that takes an initial message (task instruction) and completes the task using computer use.
•⁠  ⁠For example: ⁠ python main.py --message "open chrome and google weather in SF" ⁠
    - runs the agent loop
    - prints steps (LLM outputs, debug information)

*Part 2*

We plan to use Kubernetes to spin up computer-use agents on demand (currently we spin up full Windows VMs). As a first step, help us test this approach by implementing a simple setup that allows the GPT-5.4 agent to run in a container on Kubernetes. To observe what the agents are doing, we should be able to connect to the container via VNC or a similar interface.

Ideally, by the end of the day we have:

⁠ python main.py --message "open chrome and google weather in SF" ⁠

•⁠  ⁠spins up a container
•⁠  ⁠prints connection information so we can connect via VNC and observe the UI
•⁠  ⁠runs the agent loop
•⁠  ⁠prints steps (LLM outputs, debug information)

*Remarks*

•⁠  ⁠This might be helpful: https://developers.openai.com/api/docs/guides/tools-computer-use/?computer_use_action_handlers=docker&code_execution_harness_examples=python#option-1-run-the-built-in-computer-use-loop
•⁠  ⁠Create a github repo and push your code at the end of the trial workday.
