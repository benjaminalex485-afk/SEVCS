import os

ui_dir = r'c:\Users\benjamin.ka\Documents\AGWS\EV\SEVCS\ui'
output_file = r'c:\Users\benjamin.ka\Documents\AGWS\EV\SEVCS\ev_charging_esp32_flash\ui_bundle.h'

header_content = """#ifndef UI_BUNDLE_H
#define UI_BUNDLE_H

#include <Arduino.h>

struct UIFile {
    const char* path;
    const char* content;
    const char* mimeType;
};

"""

files_list = []

def get_mime(path):
    if path.endswith('.html'): return 'text/html'
    if path.endswith('.css'): return 'text/css'
    if path.endswith('.js'): return 'application/javascript'
    return 'text/plain'

print("Bundling UI files...")

for root, dirs, files in os.walk(ui_dir):
    for file in files:
        full_path = os.path.join(root, file)
        rel_path = os.path.relpath(full_path, ui_dir).replace('\\', '/')
        
        with open(full_path, 'r', encoding='utf-8') as f:
            content = f.read()
            
        # Escape backslashes and quotes for C++ raw string
        var_name = rel_path.replace('.', '_').replace('/', '_').replace('-', '_')
        header_content += f'const char UI_FILE_{var_name}[] PROGMEM = R"rawliteral({content})rawliteral";\n\n'
        files_list.append((rel_path, var_name, get_mime(rel_path)))

header_content += "const UIFile ui_files[] = {\n"
for path, var_name, mime in files_list:
    header_content += f'    {{"/{path}", UI_FILE_{var_name}, "{mime}"}},\n'
header_content += "};\n\n"
header_content += f"const int ui_file_count = {len(files_list)};\n\n"
header_content += "#endif"

with open(output_file, 'w', encoding='utf-8') as f:
    f.write(header_content)

print(f"Successfully generated {output_file} with {len(files_list)} files.")
