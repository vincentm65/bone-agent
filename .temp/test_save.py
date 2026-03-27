#!/usr/bin/env python3
"""Test that ConfigManager saves actually persist to disk."""
import sys
sys.path.insert(0, 'src')

from core.config_manager import ConfigManager as CM

cm = CM()

# Test model save
data = cm.load(force_reload=True)
original = data.get('GLM_MODEL', 'MISSING')
print(f"Before: GLM_MODEL = {original}")

cm.set_model('glm', 'TEST-SAVE-CHECK')
data2 = cm.load(force_reload=True)
print(f"After set_model: GLM_MODEL = {data2.get('GLM_MODEL', 'MISSING')}")

# Restore
cm.set_model('glm', original)
data3 = cm.load(force_reload=True)
print(f"After restore: GLM_MODEL = {data3.get('GLM_MODEL', 'MISSING')}")

# Test price save
cm.set_model_price('TEST-MODEL', 1.5, 2.5)
data4 = cm.load(force_reload=True)
prices = data4.get('MODEL_PRICES', {})
print(f"Price saved: {prices.get('TEST-MODEL', 'MISSING')}")

# Clean up
cm.delete_model_price('TEST-MODEL')
data5 = cm.load(force_reload=True)
prices5 = data5.get('MODEL_PRICES', {})
print(f"Price deleted: {prices5.get('TEST-MODEL', 'GONE')}")
