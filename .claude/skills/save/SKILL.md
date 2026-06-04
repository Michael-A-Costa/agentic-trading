---
name: save
description: Save conversation responses to a markdown file
---

# /save - Save Responses to Markdown

Save Claude's responses from the current conversation to a markdown file.

## Usage

```
/save                                    # Save last response to ./saved-response.md
/save output.md                          # Save last response to specified file
/save -n 3                               # Save last 3 responses
/save -n 5 -q                            # Save last 5 responses with user questions
/save notes.md -n 2 -q                   # Save last 2 exchanges to notes.md
```

## Arguments

| Argument | Description | Default |
|----------|-------------|---------|
| `[output]` | Output file path (positional) | `./saved-response.md` |
| `-n <count>` | Number of responses to include | `1` |
| `-q` | Include user questions/input | `false` (responses only) |

## Instructions

When the user invokes `/save [output] [-n count] [-q]`:

1. **Parse arguments**:
   - Extract output path (first non-flag argument, or default to `saved-response.md`)
   - Extract `-n` value for response count (default: 1)
   - Check for `-q` flag for including user questions

2. **Gather conversation content**:
   - Look back through the conversation history
   - Collect the last N assistant responses (before this /save invocation)
   - If `-q` flag is set, also include the corresponding user messages

3. **Format the markdown**:
   ```markdown
   # Saved Conversation

   *Saved on: YYYY-MM-DD HH:MM*

   ---

   ## Response 1

   [If -q flag: ### User Question\n\n{user message}\n\n### Response\n\n]
   {assistant response}

   ---

   ## Response 2
   ...
   ```

4. **Write the file**:
   - Use the Write tool to save the formatted markdown
   - If path is relative, save relative to current working directory
   - If directory doesn't exist, create it

5. **Confirm to user**:
   - Report the file path saved
   - Report how many responses were included
   - Note if user questions were included

## Examples

```bash
# Save the investigation results we just discussed
/save STELLA_INVESTIGATION.md

# Save the last 3 code reviews with context
/save code-review-notes.md -n 3 -q

# Quick save of last response
/save

# Save to a specific directory
/save docs/meeting-notes.md -n 2
```

## Notes

- Responses are ordered chronologically (oldest first)
- Code blocks, tables, and formatting are preserved
- The current /save invocation is not included in the saved content
- If fewer responses exist than requested, saves all available
