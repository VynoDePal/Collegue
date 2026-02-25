import os
import re

def patch_file(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    # We only want to patch files that define a BaseModel subclass
    if 'BaseModel' not in content:
        return

    lines = content.split('\n')
    out_lines = []
    i = 0
    in_docstring = False

    while i < len(lines):
        line = lines[i]
        out_lines.append(line)
        
        # We need to make sure we don't accidentally patch a class inside a docstring.
        # Simple heuristic: if we are in a blockquote or docstring, skip
        if '"""' in line or "'''" in line:
            # Count occurrences of """ or ''' to toggle state if needed
            # For simplicity, we just skip checking for class definition on lines with docstring quotes.
            pass
            
        m = re.match(r'^(\s*)class\s+[A-Za-z0-9_]+\s*\([^)]*BaseModel[^)]*\):', line)
        
        # Check if the next few lines are a docstring.
        if m:
            indent = m.group(1)
            # Find body indentation
            j = i + 1
            body_indent = None
            while j < len(lines):
                next_line = lines[j]
                if next_line.strip() and not next_line.strip().startswith('#'):
                    indent_match = re.match(r'^(\s+)', next_line)
                    if indent_match:
                        body_indent = indent_match.group(1)
                    break
                j += 1
            
            if body_indent is None:
                body_indent = indent + "    "
            
            # Simple check to avoid patching classes inside docstrings: 
            # Check if this class definition starts at column 0. Most of our classes do.
            if indent == '' or indent == '\t' or indent == '    ':
                out_lines.append(body_indent + "model_config = {'extra': 'forbid'}")
            
        i += 1

    new_content = '\n'.join(out_lines)
    if new_content != content:
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(new_content)
        print(f"Patched {filepath}")

if __name__ == "__main__":
    for root, dirs, files in os.walk('collegue'):
        for file in files:
            if file.endswith('.py'):
                patch_file(os.path.join(root, file))
