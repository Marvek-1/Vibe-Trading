import importlib.util, sys, os, glob, pathlib

# try to locate vibe_trading and langchain_ollama spec
for mod in ['vibe_trading', 'langchain_ollama', 'langchain_ollama.chat_models']:
    spec = importlib.util.find_spec(mod)
    if spec:
        print(f"{mod}: {spec.origin or spec.submodule_search_locations}")

# also list site-packages directories
print("--- site-packages ---")
for p in sys.path:
    if 'site-packages' in p and os.path.isdir(p):
        print(p)
        for fn in glob.glob(os.path.join(p, 'langchain_ollama*')):
            print('  found', fn)
