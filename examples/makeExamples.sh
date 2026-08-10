cd examples
python3 -m venv ../.venv
source ../.venv/bin/activate

export GOOGLE_APPLICATION_CREDENTIALS="/Users/bg/Documents/springbot-co-za-75268ec6ed1a.json"
python generate_examples_index.py

deactivate