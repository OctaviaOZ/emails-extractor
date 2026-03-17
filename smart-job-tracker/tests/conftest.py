import pytest
import resource
import sys
import os
import logging
import tracemalloc
from unittest.mock import patch

# Configure logging to show our memory reports
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def set_memory_limit(max_mem_mb):
    """
    Limits the virtual memory address space of the process to prevent system freezes.
    This works on Linux/Unix systems.
    """
    if sys.platform != 'linux':
        return
    
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)

        max_mem_bytes = int(max_mem_mb * 1024 * 1024)

        if hard != resource.RLIM_INFINITY and max_mem_bytes > hard:
            max_mem_bytes = hard

        resource.setrlimit(resource.RLIMIT_AS, (max_mem_bytes, hard))
        print(f"\n[System Safety] Memory limit set to {max_mem_mb} MB to prevent freezing.")
        
    except ValueError as e:
        logger.warning(f"Failed to set memory limit: {e}")
    except Exception as e:
        logger.warning(f"Could not set memory limit: {e}")

@pytest.fixture(scope="session", autouse=True)
def global_memory_safety():
    """
    Global fixture to enforce memory limits and prevent system freeze.
    Default limit is 16GB (16384MB). Can be overridden by TEST_MEMORY_LIMIT_MB env var.
    """
    limit_mb = int(os.getenv("TEST_MEMORY_LIMIT_MB", 16384))
    set_memory_limit(limit_mb)

@pytest.fixture(autouse=True)
def mock_local_llama_by_default():
    """
    Automatically mock the Llama class to prevent loading the heavy model during tests.
    This is the most effective way to prevent the freeze.
    """
    # Patch where it is imported in the application code
    with patch("app.services.extractor.Llama") as mock_llama:
        if mock_llama is None:
            yield None
            return

        print("\n[Test Safety] Mocking Llama model to prevent memory freeze.")
        mock_instance = mock_llama.return_value
        
        # Setup a default dummy response for the model
        mock_instance.create_completion.return_value = {
            "choices": [
                {
                    "text": '{"company_name": "Test Corp", "status": "Applied", "summary": "Test Summary", "is_rejection": false, "next_step": null}'
                }
            ],
            "usage": {"total_tokens": 10}
        }
        
        yield mock_llama

@pytest.fixture
def memory_profile(request):
    """
    Fixture to profile memory usage of a specific test.
    Usage: def test_something(memory_profile): ...
    """
    tracemalloc.start()
    
    yield
    
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    peak_mb = peak / 1024 / 1024
    
    print(f"\n[Memory Profile] {request.node.name}: Peak Memory Usage: {peak_mb:.2f} MB")