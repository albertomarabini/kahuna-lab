import json
import re
import copy
import traceback
from typing import List
import yaml
import commentjson
from collections import defaultdict

class JSON_MAIN_VALIDATION_ERROR(Exception):
    pass

class SchemaManager:
    def __init__(self, llm):
        self.llm = llm
        pass

    def color_print(self, text, color=None, end_value=None):
        COLOR_CODES = {
            'black': '30', 'red': '31', 'green': '32', 'yellow': '33', 'blue': '34', 'magenta': '35',
            'cyan': '36', 'white': '37', 'bright_black': '90', 'bright_red': '91', 'bright_green': '92',
            'bright_yellow': '93', 'bright_blue': '94', 'bright_magenta': '95', 'bright_cyan': '96', 'bright_white': '97'
        }
        if color and color.lower() in COLOR_CODES:
            color_code = COLOR_CODES[color.lower()]
            start = f"\033[{color_code}m"
            end = "\033[0m"
            text = f"{start}{text}{end}"
        if end_value == None:
            print(text, flush=True)
        else:
            print(text, end=end_value, flush=True)
        return False

    def clean_triple_backticks(self, code) -> str:
        pattern = r'```[a-zA-Z]*\n?|```\n?'
        return re.sub(pattern, '', code)

    def load_fault_tolerant_json(self, json_str, ensure_ordered=False):
        """
        Attempts to load a JSON-like string using pyyaml for fault tolerance.
        Returns a dictionary if successful; otherwise, returns an empty dictionary.
        """
        def sanitize_json_string(input_str):
            """
            Sanitizes a JSON string containing C-like syntax.
            - Escapes problematic characters inside strings.
            - Removes comments safely without altering string content.
            - Preserves the integrity of embedded C-like code.
            Args:
                input_str (str): The raw JSON string to sanitize.
            Returns:
                str: A sanitized JSON string.
            """

            def process_string_segment(match):
                """
                Processes and sanitizes individual JSON string segments.
                - Handles escape sequences and literal newlines inside strings.
                """
                content = match.group(1)  # Extract the string content without surrounding quotes

                # Step 1: Escape unescaped backslashes not part of escape sequences
                content = re.sub(r'(?<!\\)\\(?![bfnrtu"\\/])', r'\\\\', content)

                # Step 2: Replace literal newlines within the string content
                content = re.sub(r'(?<!\\)\n', r'\\n', content)

                # Step 3: Escape unescaped double quotes inside strings
                content = re.sub(r'(?<!\\)"', r'\"', content)

                # Wrap the sanitized content back in double quotes
                return f'"{content}"'

            # Step 1: Remove comments outside of strings
            def remove_comments(input_str):
                """
                Removes comments (single-line // and multi-line /* */) from JSON,
                while ignoring comments inside string literals.
                """
                # Remove single-line comments
                no_single_line_comments = re.sub(r'//.*?$|/\*.*?\*/', '', input_str, flags=re.MULTILINE | re.DOTALL)
                return no_single_line_comments

            # Step 2: Process strings to protect their content
            def sanitize_strings(input_str):
                """
                Sanitizes JSON strings to escape problematic characters.
                """
                sanitized_json = re.sub(r'(?<!\\)"((?:[^"\\]|\\.)*?)"', process_string_segment, input_str, flags=re.DOTALL)
                return sanitized_json
            # Remove code markers
            input_str = self.clean_triple_backticks(input_str)
            # Clean code markers and comments first
            input_str = remove_comments(input_str)
            # Then sanitize strings to ensure proper escaping
            sanitized_json = sanitize_strings(input_str)

            return sanitized_json
        def load_json(json_str, ensure_ordered):
            from collections import OrderedDict
            err, data = "", None
            try:
                if ensure_ordered:
                    data = commentjson.loads(self.clean_triple_backticks(json_str), object_pairs_hook=OrderedDict)
                else:
                    data = commentjson.loads(self.clean_triple_backticks(json_str))
                return data, ""
            except Exception as e:
                err = str(e)
                data = None
            try:
                data = yaml.safe_load(sanitize_json_string(json_str))
                if isinstance(data, str):
                    raise Exception("load_fault_tolerant_json: YAML parsing failed.")
                return data, ""
            except Exception as e:
                err += "\n--\n" + str(e)
                data = None
            return data, err

        data, err = load_json(json_str, ensure_ordered)
        if data:
            return data
        from json_repair import repair_json
        repaired_json_str = repair_json(json_str)
        r_data, r_err = load_json(repaired_json_str, ensure_ordered)
        if r_data:
            return r_data
        self.color_print(f"load_fault_tolerant_json: JSON parsing failed: {r_err}. \nTrying LLM recovery...", color="red")
        prompt = f"""
I encountered an issue while parsing the following JSON data. Here is the original JSON string:
```
{json_str}
```
The error message was: {r_err}
Can you fix it?

Please return the corrected JSON string and nothing else, as further comments would screw up the JSON parsing.
If you think the JSON is correct, please return the JSON as it is. Again, no further comments.
Thanks :)
        """
        repaired_json_str = self.llm.invoke(prompt)
        r_data, r_err = load_json(repaired_json_str, ensure_ordered)
        if r_data:
            return r_data
        raise Exception(f"load_fault_tolerant_json: JSON parsing failed: {r_err} \n- Original JSON: {json_str}")

    def dump_fault_tolerant_json(self, data):
        """
        Dumps a dictionary to a JSON-formatted string using pyyaml.
        Returns the JSON string if successful; otherwise, returns an empty JSON object.
        """
        try:
            # Convert data to JSON-compatible format using json.dumps after loading with yaml
            json_data = json.dumps(data, ensure_ascii=False, indent=4)
            return json_data
        except Exception as e:
            raise Exception(f"dump_fault_tolerant_json: Failed to convert data to JSON: {e}")


    def get_fuzzy_nested_node(self, schema_json, path_list: List, create_missing_tree=False, max_depth=2):
        """
        Fuzzy search for `path_list` within `schema_json` up to `max_depth` levels.
        Creates nodes as needed if `create_missing_tree=True` to reach the target path or returns the last found node.
        """
        depth_level = max_depth
        # Find the starting node by checking the schema up to `max_depth` levels
        # Start by checking if `path_list[0]` or `path_list[0] + "." + path_list[1]` exists in `schema_json`
        if len(path_list) == 0 or (len(path_list) == 1 and (path_list[0] == "" or path_list[0] == "$")):
            return schema_json
        if path_list[0] == "$":
            path_list.pop(0)  # Remove the "$" placeholder
        if len(path_list) == 0:
            return schema_json
        if path_list[0] not in schema_json:
            combined_path = f"{path_list[0]}.{path_list[1]}" if len(path_list) > 1 else path_list[0]
            if combined_path in schema_json:
                path_list[0] = combined_path  # Update `path_list[0]` to the combined name
            else:
                # If neither is found, proceed with the standard fuzzy search within depth levels
                while path_list[0] not in schema_json and depth_level > 0:
                    schema_json = schema_json.get(next(iter(schema_json), {}), {})
                    depth_level -= 1

                if path_list[0] not in schema_json:
                    return None

        # Traverse down the path list, creating nodes if needed
        if not isinstance(schema_json, dict):
            return None
        current = schema_json.get(path_list[0], None)
        for part in path_list[1:]:
            if not isinstance(current, dict):
                pass
            if part not in current:
                if create_missing_tree:
                    current[part] = {}
                    self.color_print(f"Creating missing path node '{part}' within '{'.'.join(path_list)}'", color="bright_cyan")
                else:
                    return current  # For delete, skip further creation
            else:
                if not isinstance(current[part], dict) and part == path_list[-1] and create_missing_tree:
                    current[part] = {}
            current = current[part]

        return current

    def apply_commands_to_schema(
        self,
        schema,
        command,
        pseudocode_schema_json
    ):
        def validate_schema(label, pseudocode_schema_json, schema_json, discrepancies, old_schema_json, path, invalidate_process):
            schema_validation_errors = self.validate_schema(pseudocode_schema_json, self.dump_fault_tolerant_json(schema_json))
            if schema_validation_errors:
                flattened_schema_validation_errors = "\n".join([f"\"- path:`{v['path']}`: {v['error']}\"" for v in schema_validation_errors])
                self.color_print(
                    f"{label} at path {'.'.join(path)} caused {len(schema_validation_errors)} validation errors",
                    color="bright_blue",
                )
                discrepancies.append(f"{label} at path: {'.'.join(path)} caused the following validation errors:\n{flattened_schema_validation_errors}\n\n")
                invalidate_process = True
            if invalidate_process == True:
                schema_json = copy.deepcopy(old_schema_json)
            return schema_json, discrepancies, invalidate_process

        def process_escaped_newlines(json_string):
            def process_string_segment(match):
                content = match.group(1)
                parts = content.split('\\n')
                processed_parts = [part.replace('\n', '\\n') for part in parts]
                processed_content = '\\n'.join(processed_parts)
                return f'"{processed_content}"'

            processed_json = re.sub(r'(?<!\\)"((?:[^"\\]|\\.)*?)"', process_string_segment, json_string, flags=re.DOTALL)
            return processed_json

        def flatten_nested_arrays(base_key, value, consolidated_commands):
            """
            Dynamically flattens nested arrays into their base key.
            I understand is ugly but sometimes the nesting from the LLM comes back wrong, with objects nasted inside objects like parasite evil twins :)
            This sorta tries to fix it out :)
            """
            if isinstance(value, list):  # Ensure it's a list of items
                to_remove = []  # Track items to remove after processing
                for item in value:
                    if isinstance(item, dict):  # Salvaging nested keys
                        for nested_key in list(item.keys()):  # Use list to allow modification
                            if any(nested_key.startswith(base) for base in {'insert', 'update', 'delete'}):
                                base_nested_key = re.sub(r'\d+$', '', nested_key)  # Extract base key (e.g., 'update' from 'update2')
                                nested_value = item.pop(nested_key)
                                consolidated_commands[base_nested_key].extend(
                                    nested_value if isinstance(nested_value, list) else [nested_value]
                                )
                                self.color_print(f"Json odd case, salvaged nested key '{nested_key}' into root as '{base_nested_key}'.", color="bright_blue")
                        # If the item is now empty, mark it for removal
                        if not item:
                            to_remove.append(item)
                # Remove empty items from the original list
                for empty_item in to_remove:
                    value.remove(empty_item)
            return consolidated_commands

        def parse_and_merge_duplicates(command_string):
            """
            Parses JSON with duplicate keys, consolidates them, and flattens nested arrays.
            """
            # Step 1: Identify duplicates and rename them
            label_counts = defaultdict(int)
            pattern = re.compile(r'"\b(insert|update|delete)\b"\s*:')

            def rename_label(match):
                label = match.group(1)
                label_counts[label] += 1
                return f'"{label}{label_counts[label]}" :'

            try:
                # Apply renaming of duplicate labels
                renamed_string = re.sub(pattern, rename_label, command_string)

                # Step 2: Load the JSON with renamed labels
                loaded_json = self.load_fault_tolerant_json(renamed_string)

                # Step 3: Consolidate commands with the same base label
                consolidated_commands = defaultdict(list)

                # Go through renamed labels and append to the base label
                if not isinstance(loaded_json, dict):
                    raise ValueError(f"The loaded JSON is not a dictionary.{loaded_json}")
                for key, value in loaded_json.items():
                    base_key = re.sub(r'\d+$', '', key)  # Strip numeric suffix
                    if base_key in {'insert', 'update', 'delete'}:
                        # Flatten nested arrays before consolidating
                        consolidated_commands = flatten_nested_arrays(base_key, value, consolidated_commands)
                        consolidated_commands[base_key].extend(value if isinstance(value, list) else [value])
                    else:
                        consolidated_commands[key] = value  # Non-command keys go directly

                # Convert defaultdict back to a regular dict for final JSON
                final_command_json = dict(consolidated_commands)
                return final_command_json
            except Exception as e:
                raise ValueError(f"Error during parsing and merging: {e}\n{command_string}\n{traceback.format_exc()}") from e

        def parse_multiple_json_objects(command_string):
            """
            Parses multiple JSON objects separated by markdown-like delimiters.
            """
            commands = []
            json_blocks = re.split(r"```json|```|'''json|'''", command_string)

            for json_str in json_blocks:
                json_str = json_str.strip()
                if not json_str or json_str == "": # Don't ask, don't tell
                    continue  # Skip empty blocks
                try:
                    command = parse_and_merge_duplicates(process_escaped_newlines(json_str))
                    commands.append(command)
                except Exception as e:
                    self.color_print(f"Skipping invalid JSON command:\n{json_str}\nError: {e}\n{traceback.format_exc()}", color="red")
                    raise ValueError("Skipping invalid JSON command:{json_str}") from e
            return commands

        # Utility function for recursive merging of dictionaries
        def recursive_update(target: dict, source: dict):
            for key, value in source.items():
                if isinstance(value, dict) and isinstance(target.get(key), dict):
                    recursive_update(target[key], value)
                else:
                    target[key] = value

        def extract_path(item):
            """
            Extract the first 'path' key in a nested object, considering that object as 'item' or the first string that looks like a path (is a mad world).
            """

            def find_path_key(obj):
                """Recursively search for the first occurrence of a 'path' key."""
                if isinstance(obj, dict):
                    if "path" in obj and isinstance(obj["path"], str):
                        return obj  # Found the object containing 'path'
                    for value in obj.values():
                        result = find_path_key(value)
                        if result:
                            return result
                elif isinstance(obj, list):
                    for element in obj:
                        result = find_path_key(element)
                        if result:
                            return result
                return None

            # Find the object containing 'path'
            found_item = find_path_key(item)

            # Extract the path if found
            try:
                if isinstance(item, str):
                    item = {"path": item}
                    path = item["path"].split(".")
                    return path, item
                elif found_item and "path" in found_item and isinstance(found_item["path"], str):
                    path = found_item["path"].split(".")
                    return path, found_item
                elif path := next((value for value in item.values() if isinstance(value, str) and ("$" in value or str(value).count(".") > 1)), None):
                    item["path"] = path
                    path = path.split(".")
                    return path, item
            except Exception as e:
                return [], item

            # If no valid 'path' key is found, raise an error
            raise ValueError(f"Unable to locate 'path' in the item: {item}")


        def diff(obj1, obj2):
            if isinstance(obj1, dict) and isinstance(obj2, dict):
                if set(obj1.keys()) != set(obj2.keys()):
                    return False
                for key in obj1.keys():
                    if not diff(obj1[key], obj2[key]):
                        return False
                return True
            elif isinstance(obj1, str) and isinstance(obj2, str):
                return obj1 == obj2
            else:
                return False
        def is_subset(small_dict, large_dict):
            if not small_dict:
                return False
            return set(small_dict.keys()).issubset(large_dict.keys())


        # Loading the main schema
        try:
            schema_json = self.load_fault_tolerant_json(process_escaped_newlines(re.sub(r"```json|```|'''json|'''", "", schema).strip()))
        except Exception as e:
            self.color_print(f"apply_commands_to_schema - Error During Development: The loaded schema is not a valid json {e}\n{schema}", color="bright_red")
            raise JSON_MAIN_VALIDATION_ERROR()
        if command == None:
            return schema_json, [], []

        commands = parse_multiple_json_objects(command)
        target, processed_paths, discrepancies, initial_schema, invalidate_process = None, [], [], schema, False
        old_schema_json = copy.deepcopy(schema_json)
        try:
            # for command_obj in commands: # Don't ask don't tell
            i = 0
            while i < len(commands):
                command_obj = commands[i]
                i += 1
                try:
                    # Process inserts
                    for item in command_obj.get("insert", []):
                        try:
                            path, item = extract_path(item)
                            target = self.get_fuzzy_nested_node(schema_json, path[:-1], create_missing_tree=True)
                            if not item.get("content", None):
                                self.color_print(f"Skipping insert at path: {path} as the content part of the command received is missing", color="bright_cyan")
                                discrepancies.append(f"Insert at path: {'.'.join(path)} failed. No `content` part was provided within the command.")
                                invalidate_process = True
                                continue
                            if not isinstance(target, dict):
                                if path[0] != "$":
                                    path.insert(0, "$")
                                combinations = " ".join([f"Does `{l}` esists?" for l in ['.'.join(path[:i]) for i in range(2, len(path))]])
                                self.color_print(f"Skipping insert, object {".".join(path[:-1])} not found", color="bright_cyan")
                                discrepancies.append(f"Insert at path: {".".join(path)} failed. Object {".".join(path[:-1])} not found: it must be created along with the entity you are trying to insert." + combinations)
                                invalidate_process = True
                                continue
                            if not path[-1] in target:
                                target[path[-1]] = item["content"]
                                schema_json, discrepancies, invalidate_process = validate_schema("Inserting", pseudocode_schema_json, schema_json, discrepancies, old_schema_json, path, invalidate_process)
                                if not invalidate_process:
                                    print(f"Inserting at path: {path}", flush=True)
                                if not ".".join(path) in processed_paths:
                                    processed_paths.append(".".join(path))
                            else:
                                if not "update" in command_obj:
                                    command_obj["update"] =[]
                                command_obj["update"].append({"path": item["path"], "content": item["content"]})
                        except Exception as e:
                            self.color_print(f"apply_commands_to_schema - inserts - Error During Development: Insert Error: {e}\n{traceback.format_exc()}\nPath attempted: '{item['path']}'\nComplete Command:\n{item}\n", color="red")

                    # Process updates
                    for item in command_obj.get("update", []):
                        # if len(path) < 2:
                        #     self.color_print(f"Unprocessed Update at path: {path}", color="blue")
                        #     continue
                        try:
                            path, item = extract_path(item)
                            target = self.get_fuzzy_nested_node(schema_json, path[:-1], create_missing_tree=True)  # Use create=True for partial inserts
                            if target == None:
                                self.color_print(f"Skipping update at path: {path} one of `{path[-1]}`'s ancestors does not exist", color="bright_cyan")
                                discrepancies.append(f"Update at path: {".".join(path)} failed. One of `{path[-1]}`'s ancestors does not exist in the current schema. Must be created before or along `{path[-1]}` to proceed with this operation.")
                                invalidate_process = True
                                continue
                            if not isinstance(target, dict):
                                self.color_print(f"Skipping update at path: {path} the target's parent in not an entity", color="bright_cyan") #Not exists if == None?
                                discrepancies.append(f"Update at path: {".".join(path)} failed. {".".join(path[:-1])} is a string attribute, not an entity.")
                                invalidate_process = True
                                continue
                            if not item.get("content", None):
                                self.color_print(f"Skipping update at path: {path} as the content is missing", color="bright_cyan")
                                discrepancies.append(f"Update at path: {".".join(path)} failed. No Content field was provided within the command.")
                                invalidate_process = True
                                continue
                            if isinstance(item["content"], str):
                                if target.get(path[-1],None) and not isinstance(target[path[-1]], str):
                                    self.color_print(f"Skipping update at {path}: {path[-1]} is not a string attribute.", color="bright_cyan")
                                    discrepancies.append(f"Update at path: {".".join(path)} failed. {path[-1]} is not a string attribute.")
                                    invalidate_process = True
                                    continue
                                if target.get(path[-1],None) and target[path[-1]] == item["content"]:
                                    self.color_print(f"Update at path {path}: content match.", color="bright_cyan")
                                target[path[-1]] = item["content"]
                            elif isinstance(item["content"], dict):
                                # Trying to catch some edge cases to accomodate for LLM quirks
                                if path[-1] not in target:
                                    self.color_print(f"Update odd case 1: path[-1] not in target.", color="bright_cyan")
                                    target[path[-1]] = item["content"]
                                elif isinstance(target[path[-1]], str):
                                    # was some other mismatching odd case
                                    if is_subset(item["content"], target):
                                        self.color_print(f"Update odd case 2: is_subset(item['content'], target)", color="bright_cyan")
                                        recursive_update(target, item["content"])
                                    elif path[-1] in item["content"]:
                                        self.color_print(f"Update odd case 3:path[-1] in item['content']", color="bright_cyan")
                                        target[path[-1]] = item["content"][path[-1]]
                                    elif len(path) > 2 and path[-2] in item["content"] and isinstance(item["content"][path[-2]], dict) and is_subset(item["content"][path[-2]], target):
                                        self.color_print(f"Update real odd case 4.", color="bright_cyan")
                                        recursive_update(target, item["content"][path[-2]])
                                    else:
                                        # Ok, this sucks, but what if EOD i'm trying to update a string with a dict?
                                        self.color_print(f"Update odd case uncaught path: {path} Target does not match the content type.", color="blue")
                                        discrepancies.append(f"Update at path: {path} failed. Target does not match the content type.")
                                else:
                                    # If all is good, just update
                                    if diff(target[path[-1]], item["content"]):
                                        self.color_print(f"Update at path {path}:content match.", color="bright_cyan")
                                    recursive_update(target[path[-1]], item["content"])
                            if not ".".join(path) in processed_paths:
                                processed_paths.append(".".join(path))
                            schema_json, discrepancies, invalidate_process = validate_schema("Updating", pseudocode_schema_json, schema_json, discrepancies, old_schema_json, path, invalidate_process)
                            if not invalidate_process:
                                print(f"Update at path: {path}", flush=True)
                        except Exception as e:
                            self.color_print(f"apply_commands_to_schema - updates - Error During Development: Update Error: {e}\n{traceback.format_exc()}\nPath attempted: '{item['path']}'\nComplete Command:\n{item}\n{traceback.format_exc()}\n", color="red")

                    # Process deletions
                    for item in command_obj.get("delete", []):
                        try:
                            path, item = extract_path(item)
                            if item["path"] in processed_paths:
                                self.color_print(f"Skipping delete at path: {item['path']} as previously processed", color="bright_cyan")
                                continue
                            # if len(path) < 2:
                            #     self.color_print(f"Unprocessed Delete at path: {path}", color="bright_cyan")
                            #     continue
                            target = self.get_fuzzy_nested_node(schema_json, path[:-1], create_missing_tree=False)  # Don't create paths for deletion
                            if not isinstance(target, dict) or not path[-1] in target:
                                self.color_print(f"Delete at path {path}:the target does not exist", color="bright_cyan")
                                # discrepancies.append(f"Delete at path: {path} failed. Target does not exist.")  #Can we live and let live? Not sure.
                                continue
                            if len(path) >= 2 and path[-1] in {"body", "description"}:
                                self.color_print(
                                    f"Interpreting delete of '{path[-1]}' at path {path} as delete of entity '{path[-2]}'",
                                    color="bright_cyan"
                                )
                                # Rewrite path to point to the parent entity
                                path = path[:-2] + [path[-2]]
                            target.pop(path[-1], None)
                            schema_json, discrepancies, invalidate_process = validate_schema("Deleting", pseudocode_schema_json, schema_json, discrepancies, old_schema_json, path, invalidate_process)
                        except Exception as e:
                            self.color_print(f"apply_commands_to_schema - deletes - Error During Development: Delete Error: {e}\n{traceback.format_exc()}\nPath attempted: '{item['path']}'\nComplete Command:\n{item}\n", color="red")
                        if not invalidate_process:
                            print(f"Deleting at path: {path}", flush=True)

                    # Overall load/unload test
                    try:
                        updated_schema_text = self.dump_fault_tolerant_json(schema_json)
                        schema_json = self.load_fault_tolerant_json(process_escaped_newlines(re.sub(r"```json|```|'''json|'''", "", updated_schema_text).strip()))
                    except Exception as e:
                        schema_json = old_schema_json
                except Exception as e:
                    print(f"Traceback details:\n{traceback.format_exc()}")
                    discrepancies.append(f"An Exception was raised while parsing the command: {e}")
                    self.color_print(f"apply_commands_to_schema - Error During Development: {e}\n{traceback.format_exc()}\nCurrent command: {command_obj}\n", color="red")
        except Exception as e:
            print(f"Traceback details:\n{traceback.format_exc()}")
            discrepancies.append(f"An Exception was raised while parsing the command: {e}")
            self.color_print(f"apply_commands_to_schema - Error During Development: {e}\n{traceback.format_exc()}\nCurrent commands: {str(commands)}\n", color="red")
        if invalidate_process:
            return initial_schema, discrepancies, []
        updated_schema_text = self.dump_fault_tolerant_json(schema_json)
        return updated_schema_text, discrepancies, processed_paths


    ##########################################################################################
    # ** Schema Validation **
    ##########################################################################################


    def validate_schema(
        self,
        pseudocode_schema_json,
        schema
        ):
        discrepancies = []  # Collects all validation errors

        def is_entity_node(schema_node, json_node=None, deep = False):
            if json_node is None:
                if not isinstance(schema_node, dict):
                    return False
                return any(isinstance(value, str) for value in schema_node.values())
            else:
                for key, value in schema_node.items():
                    if key not in json_node and isinstance(value, str):
                        return False
                    # This part is commented out to allow for not entirely rappresented sub entities, enforcing only attributes
                    # elif key not in json_node and isinstance(value, dict) and is_entity_node(value):
                    #     return False
            if deep:
                # Only Attributes and sub entities  detailed in the schema are allowed
                for key in json_node.keys():
                    if key not in schema_node:
                        return False
            return True

        def validate_entity_node(json_node, schema_node, path):
            already_validated = False
            for key, value in schema_node.items():
                if key not in json_node and isinstance(value, str) and len(json_node.items()) > 0:
                    discrepancies.append({
                        "path": f"{path}.{key}",
                        "error": f"Missing required attribute '{key}' in entity node '{path}'."
                    })
                    already_validated = True
                # This part is commented out to allow for not entirely rappresented sub entities, enforcing only attributes
                # elif key not in json_node and isinstance(value, dict) and is_entity_node(value):
                #     discrepancies.append({
                #         "path": f"{path}.{key}",
                #         "error": f"Missing required entity node '{key}' in entity node '{path}'."
                #     })
                #     already_validated = True
                elif key in json_node and isinstance(value, dict) != isinstance(json_node[key], dict):
                    if isinstance(value, str):
                        discrepancies.append({
                            "path": f"{path}.{key}",
                            "error": f"'{key}' is expected to be a text attribute in entity node '{path}', not a sub-entity."
                        })
                    else:
                        discrepancies.append({
                            "path": f"{path}.{key}",
                            "error": f"'{key}' is expected to be a sub-entity of the entity '{path}', not a text attribute."
                        })
                    already_validated = True
                elif key in json_node and isinstance(value, dict) and is_entity_node(value):
                    validate_entity_node(json_node[key], value, f"{path}.{key}")
                elif key in json_node and isinstance(value, dict) and not is_entity_node(value):
                    validate_grouping_node(json_node[key], value, f"{path}.{key}")
            # Only Attributes and sub entities  detailed in the schema are allowed
            for key, value in json_node.items():
                if key not in schema_node:
                    expected_fields = [f"'{k}'" for k in schema_node.keys()]
                    if isinstance(value, dict):
                        accepts_sub_entities = f"The entity {path} does not support sub-entities. Re-engineer your solution. Acceptable attribute names for {path} are:" if all(k in ('body', 'description', 'declaration') for k in schema_node.keys()) else "Acceptable sub-entity/attribute names are:"
                        discrepancies.append({
                            "path": f"{path}.{key}",
                            "error": f"Unexpected sub-entity '{key}' within entity '{path}'. {accepts_sub_entities} {', '.join(expected_fields)}"
                        })
                    else:
                        discrepancies.append({
                            "path": f"{path}.{key}",
                            "error": f"Unexpected text attribute '{key}' with value '{str(value)}' in entity node '{path}'.Acceptable child names are: {', '.join(expected_fields)}"
                        })
                    already_validated = True
            return already_validated

        def validate_grouping_node(json_node, schema_node, path):
            """
            Validate a grouping node by ensuring all child nodes are valid entities
            and conform to one of the allowed entity types in the schema.
            """
            for child_key, child_value in json_node.items():
                valid_entity_type = False  # Track whether the child matches a valid entity type
                already_validated = False
                if is_entity_node(child_value):
                    for entity_type, entity_schema in schema_node.items():
                        if is_entity_node(entity_schema): #Friendly doublecheck
                            # Check if the child matches the current entity type
                            if is_entity_node(entity_schema, child_value, False):
                                if not is_entity_node(entity_schema, child_value, True):
                                    unexpected_fields = [f"{key}" for key in child_value.keys() if key not in entity_schema]
                                    expected_fields = [f"'{key}'" for key in entity_schema.keys()]
                                    is_module = path == "$" and all(k in ('filename', 'language', 'framework') for k in entity_schema.keys())
                                    has_grouping_nodes = any(isinstance(value, dict) for value in entity_schema.values())
                                    is_unexpected_field_an_entity = any(isinstance(child_value[key], dict) for key in unexpected_fields)
                                    entity_type = "Module" if is_module else "Entity"
                                    unexpected_fields = [f"'{key}'" for key in unexpected_fields]
                                    error_str = ""
                                    if has_grouping_nodes and is_unexpected_field_an_entity:
                                        error_str = f"{entity_type} '{child_key}' cannot contain the sub-entiti(es) '{', '.join(unexpected_fields)}'. Did you missed a containing grouping node?"
                                    elif not has_grouping_nodes and is_unexpected_field_an_entity:
                                        error_str = f"{entity_type} '{child_key}' cannot contain any sub_entities. The sub-entiti(es) '{', '.join(unexpected_fields)}' are not allowed."
                                    elif not is_unexpected_field_an_entity:
                                        error_str = f"{entity_type} '{child_key}' cannot contain the text attribute(s) '{', '.join(unexpected_fields)}'."
                                    else:
                                        error_str = f"{entity_type} '{child_key}' cannot contain entities/attributes with the following names: {", ".join(unexpected_fields)}."
                                    error_str += f" Acceptable child names are: {', '.join(expected_fields)}."
                                    discrepancies.append({
                                        "path": f"{path}.{child_key}",
                                        "error": error_str
                                    })
                                    already_validated= True
                                    valid_entity_type = False
                                    break
                                else:
                                    valid_entity_type = True
                                    already_validated = validate_entity_node(child_value, entity_schema, f"{path}.{child_key}")
                                    break  # No need to check further types for this child

                    if not valid_entity_type and not already_validated:
                        expected_types = ",\n".join([f"'{key}':" + ("{" + ", ".join([f"'{k}':'{v}'" for k,v in schema_node[key].items()]) +"}\n") if isinstance(schema_node[key], dict) else "'<str>'\n" for key in schema_node.keys()])
                        found_type = ",\n".join([f"'{key}':{'"..."' if isinstance(child_value[key],str) else '{...}'}" for key in child_value.keys()])
                        can_contain = "can contain only modules" if(path == "$") else "can contain only sub entities/attributes"
                        discrepancies.append({
                            "path": f"{path}.{child_key}",
                            "error": f"Entity '{child_key}' Invalid Node Type: '{path}' {can_contain} with the following structures: \n{expected_types}\n**Found**'{child_key}':{found_type}"
                        })
                        already_validated= True
                else:
                    try:
                        expected_types , items_type= " or \n".join([f"'{key}':{"{" + ", ".join([f"'{k}':'{v}'" for k,v in schema_node[key].items()]) +"}"}" for key in schema_node.keys()]), path.split('.')[-1]
                        discrepancies.append({
                            "path": f"{path}.{child_key}",
                            "error": f"Entity '{child_key}' Invalid Node Type: '{path}' is a Grouping Node and cannot contain strings. Were you trying to create a '{items_type[:-1]}'? It should be formatted in the following way: {expected_types})."
                        })
                        already_validated= True
                    except Exception as e:
                        raise e

                if not valid_entity_type and not already_validated:
                    discrepancies.append({
                        "path": f"{path}.{child_key}",
                        "error": f"Entity '{child_key}' does not match any valid type for grouping node '{path}'."
                    })

        if isinstance(pseudocode_schema_json, str):
            pseudocode_schema_json = self.load_fault_tolerant_json(pseudocode_schema_json)
        schema = self.load_fault_tolerant_json(schema)
        try:
            if is_entity_node(pseudocode_schema_json):
                validate_entity_node(schema, pseudocode_schema_json, "$")
            else:
                validate_grouping_node(schema, pseudocode_schema_json, "$")
        except Exception as e:
            self.color_print(f"Error during schema validation: {e}\n{traceback.format_exc()}", color="red")

        return discrepancies
