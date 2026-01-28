# smart-job-tracker/download_model.py
import os
from huggingface_hub import hf_hub_download

def download_local_model():
    # 1. Define Model Details
    repo_id = "bartowski/Llama-3.2-3B-Instruct-GGUF"
    filename = "Llama-3.2-3B-Instruct-Q4_K_M.gguf"
    
    # 2. Define Storage Path (inside a 'models' folder in your project)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    models_dir = os.path.join(base_dir, "models")
    os.makedirs(models_dir, exist_ok=True)
    
    print(f"📂 Model directory: {models_dir}")
    print(f"⬇️  Downloading {filename} from {repo_id}...")
    
    # 3. Download
    try:
        model_path = hf_hub_download(
            repo_id=repo_id,
            filename=filename,
            local_dir=models_dir,
            local_dir_use_symlinks=False
        )
        print(f"✅ Success! Model saved to:\n   {model_path}")
        print("\nNext: Update your environment to use this path.")
        return model_path
    except Exception as e:
        print(f"❌ Error downloading model: {e}")

if __name__ == "__main__":
    download_local_model()