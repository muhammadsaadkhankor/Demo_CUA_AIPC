# CUA AIPC

AI-powered Computer Use Agent with React UI.

## Setup

### Backend
```
cd CUA_AIPC
pip install -r requirements.txt
python agent.py
```
Runs on http://localhost:5000

### Frontend
```
cd CUA_AIPC/ui
npm install
npm start
```
Opens on http://localhost:3000

## Usage
1. Make sure your vLLM model is running:
   ```
   vllm serve ByteDance-Seed/UI-TARS-2B-SFT --served-model-name ui-tars-2b --dtype float16 --max-model-len 8192 --max-num-seqs 1 --gpu-memory-utilization 0.85 --limit-mm-per-prompt '{"image": 1}' --enforce-eager
   ```
2. Start `agent.py`
3. Start the React UI
4. Type the app name (e.g. `Calculator`, `Notepad`, `Paint`)
5. Click **Launch** — the UI minimizes, the model takes over, opens the app, then restores the UI and shows the result
