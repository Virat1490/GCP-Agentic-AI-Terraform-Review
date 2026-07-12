import os
import json
import re
import glob

# Try importing Gemini SDK
try:
    import google.generativeai as genai
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False

def find_resource_code(resource_type, resource_name, search_path='.'):
    """
    Scans .tf files in the directory to find the specific resource block.
    Returns (filename, content_block) or (None, None).
    """
    # Regex to find 'resource "type" "name" {'
    # We use a non-greedy .*? to match potential flexible whitespace
    pattern = re.compile(rf'resource\s+"{re.escape(resource_type)}"\s+"{re.escape(resource_name)}"\s+\{{')
    
    for tf_file in glob.glob(os.path.join(search_path, '*.tf')):
        try:
            with open(tf_file, 'r', encoding='utf-8') as f:
                content = f.read()
            
            match = pattern.search(content)
            if match:
                # Naively find the closing brace by counting braces
                # This is a simple heuristic; a proper parser is better but complex to implement without libs
                start_index = match.start()
                open_braces = 0
                end_index = -1
                
                # effective_content starting from the match
                for i, char in enumerate(content[start_index:], start=start_index):
                    if char == '{':
                        open_braces += 1
                    elif char == '}':
                        open_braces -= 1
                        if open_braces == 0:
                            end_index = i + 1
                            break
                
                if end_index != -1:
                    return tf_file, content[start_index:end_index]
        except Exception as e:
            print(f"Error reading {tf_file}: {e}")
            continue
            
    return None, None

def get_ai_fix(api_key, violation_msg, resource_code, resource_type, resource_name):
    """
    Calls Gemini to explain the violation and suggest a fix.
    """
    if not HAS_GENAI or not api_key:
        return None

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-1.5-pro')
        
        prompt = f"""
You are a Senior Google Cloud Platform (GCP) Security Engineer and Terraform Expert.

**Context:**
A Terraform resource in our codebase has failed a security policy check (OPA).

**Resource Details:**
- Type: `{resource_type}`
- Name: `{resource_name}`

**The Violation:**
"{violation_msg}"

**The Offending Terraform Code:**
```hcl
{resource_code}
```

**Your Task:**
1.  **Analyze**: Briefly explain *why* this configuration is a security or compliance risk.
2.  **Fix**: Provide the corrected Terraform code block. 
    -   Keep the existing configuration but modify it to fix the violation.
    -   Do NOT remove unrelated valid comments/settings.
    -   ONLY return the code for the specific resource.

**Output Format:**
Provide your response in Markdown. Use a sub-header "### Fix Recommendation" for the code block.
"""
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        print(f"LLM Generation Error: {e}")
        return None

def generate_review(violations, plan, search_path='.'):
    """
    Generates a GitHub PR comment based on OPA violations.
    """
    # Check if 'result' key exists and has content
    if not violations.get('result'):
        return "✅ **Agent Review:** Infrastructure looks good! No policy violations found."

    # Parse violations
    violation_messages = []
    # OPA output structure: violations['result'][0]['expressions'][0]['value'] is a list of strings
    try:
        # Access the first result's first expression's value
        if violations['result'] and violations['result'][0].get('expressions'):
            violation_messages = violations['result'][0]['expressions'][0]['value']
    except (KeyError, IndexError, TypeError):
        return "⚠️ **Agent Review:** Errors found but could not parse details. Please check the OPA logs for raw output."
    
    if not violation_messages:
         return "✅ **Agent Review:** Infrastructure looks good! No policy violations found."

    # Map resource names to types using tfplan
    # tfplan['resource_changes'] is a list of dicts with 'address', 'type', 'name'
    resource_map = {}
    if plan.get('resource_changes'):
        for rc in plan['resource_changes']:
            resource_map[rc['name']] = rc['type']

    # Build Markdown Report
    api_key = os.environ.get('GEMINI_API_KEY')
    
    report = ["# 🤖 Agentic Terraform Review\n"]
    report.append(f"**Found {len(violation_messages)} Policy Violations**\n")
    
    for msg in violation_messages:
        # Expected msg format: "category: message text."
        parts = msg.split(':', 1)
        category = parts[0].strip() if len(parts) > 1 else "General"
        message_body = parts[1].strip() if len(parts) > 1 else msg
        
        # Extract resource name
        resource_match = re.search(r"'([^']*)'", message_body)
        resource_name = resource_match.group(1) if resource_match else "Unknown"
        resource_type = resource_map.get(resource_name, "unknown_type")
        
        report.append(f"## 🛑 Violation: {resource_name}")
        report.append(f"**Category:** `{category}`")
        report.append(f"**Message:** {message_body}\n")
        
        # Try to find code and get AI fix
        if resource_name != "Unknown" and resource_type != "unknown_type":
            tf_file, code_block = find_resource_code(resource_type, resource_name, search_path)
            
            if code_block:
                report.append(f"> **File:** `{tf_file}`\n")
                
                # Call LLM
                ai_response = get_ai_fix(api_key, msg, code_block, resource_type, resource_name)
                
                if ai_response:
                    report.append(ai_response)
                else:
                    # Fallback if no LLM or API key
                    report.append("### 🔧 Manual Fix Required")
                    report.append(f"Please update `{resource_name}` in `{tf_file}` to address the violation.")
                    report.append("```hcl")
                    report.append(code_block)
                    report.append("```")
            else:
                report.append(f"*(Could not locate source code for {resource_name})*")
        else:
            report.append("*(Could not determine resource type or name from violation)*")
            
        report.append("\n---\n")

    report.append("\n*Generated by Agentic Reviewer powered by Gemini*")
    
    return "\n".join(report)

if __name__ == "__main__":
    # Load OPA violations
    try:
        with open('opa_violations.json', 'r') as f:
            violations = json.load(f)
    except FileNotFoundError:
        print("Error: opa_violations.json not found.")
        violations = {}

    # Load Terraform Plan (simplified for context)
    try:
        with open('tfplan.json', 'r') as f:
            tf_plan = json.load(f)
    except FileNotFoundError:
        print("Error: tfplan.json not found.")
        tf_plan = {}

    review_body = generate_review(violations, tf_plan)
    print(review_body)

    # Save for GitHub Action to use
    with open('pr_comment.txt', 'w', encoding='utf-8') as f:
        f.write(review_body)
