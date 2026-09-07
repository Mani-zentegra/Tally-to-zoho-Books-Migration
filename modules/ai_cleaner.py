import re
import json
import requests

OPENROUTER_API_KEY = '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'

def clean_addresses_with_ai(addresses):
    """
    Cleans addresses using OpenRouter (LLaMA-3) to extract city, state, pincode, etc.
    """
    if not addresses:
        return []
        
    results = []
    url = 'https://openrouter.ai/api/v1/chat/completions'
    headers = {
        'Authorization': f'Bearer {OPENROUTER_API_KEY}',
        'Content-Type': 'application/json'
    }
    
    for item in addresses:
        original_address = str(item.get("address", ""))
        
        system_prompt = '''You are an expert address parser. 
Extract the City, State, Country, Pincode, Email, and Phone from the given text.
Return ONLY a valid JSON object matching exactly these keys:
{
  "clean_address": "string",
  "city": "string",
  "state": "string",
  "country": "string",
  "pincode": "string",
  "email": "string",
  "phone": "string"
}
If a value is not present in the text, leave it as an empty string "".
Do not output any markdown formatting, only the raw JSON string.
'''

        payload = {
            'model': 'meta-llama/llama-3-8b-instruct:free',
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': f'Address:\n{original_address}'}
            ],
            'response_format': {'type': 'json_object'}
        }
        
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=30)
            if r.status_code == 200:
                response_data = r.json()
                content = response_data['choices'][0]['message']['content']
                extracted = json.loads(content)
                
                results.append({
                    "id": item.get("id"),
                    "clean_address": extracted.get("clean_address", original_address),
                    "city": extracted.get("city", ""),
                    "state": extracted.get("state", ""),
                    "country": extracted.get("country", ""),
                    "pincode": str(extracted.get("pincode", "")),
                    "email": extracted.get("email", ""),
                    "phone": str(extracted.get("phone", ""))
                })
            else:
                print(f"OpenRouter Error {r.status_code}: {r.text}")
                # fallback
                results.append({
                    "id": item.get("id"),
                    "clean_address": original_address,
                    "city": "", "state": "", "country": "", "pincode": "", "email": "", "phone": ""
                })
        except Exception as e:
            print(f"AI cleaning failed for '{original_address}': {e}")
            # fallback
            results.append({
                "id": item.get("id"),
                "clean_address": original_address,
                "city": "", "state": "", "country": "", "pincode": "", "email": "", "phone": ""
            })
            
    return results

