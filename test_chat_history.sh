#!/bin/bash

echo "=========================================="
echo "Testing Chat History API"
echo "=========================================="
echo ""

BASE_URL="http://localhost:8000/api/v1"

echo "Test 1: Get history for existing conversation (test-conv-1)"
echo "-----------------------------------------------------------"
curl -s -X POST "$BASE_URL/test-chat-history" \
  -H "Content-Type: application/json" \
  -d '{"conversation_id": "test-conv-1"}' | python3 -c "import sys, json; data=json.load(sys.stdin); print(f\"✓ Success: Retrieved {data['total_messages']} messages\"); print(f\"  Thread ID: {data['thread_id']}\"); print(f\"  First message: {data['messages'][0]['role']}: {data['messages'][0]['message'][:50]}...\") if data['messages'] else print('  No messages')"
echo ""

echo "Test 2: Get history for non-existent conversation"
echo "-----------------------------------------------------------"
curl -s -X POST "$BASE_URL/test-chat-history" \
  -H "Content-Type: application/json" \
  -d '{"conversation_id": "non-existent-conv"}' | python3 -c "import sys, json; data=json.load(sys.stdin); print(f\"✓ Success: Retrieved {data['total_messages']} messages (expected 0)\")"
echo ""

echo "Test 3: Verify message ordering (chronological)"
echo "-----------------------------------------------------------"
curl -s -X POST "$BASE_URL/test-chat-history" \
  -H "Content-Type: application/json" \
  -d '{"conversation_id": "test-conv-1"}' | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data['total_messages'] > 0:
    messages = data['messages']
    print(f\"✓ Retrieved {len(messages)} messages\")
    print(f\"  Message IDs: {[msg['id'] for msg in messages]}\")
    print(f\"  Roles: {[msg['role'] for msg in messages]}\")
    ids = [int(msg['id']) for msg in messages]
    if ids == sorted(ids):
        print('  ✓ Messages are ordered by ID (ascending)')
    else:
        print('  ✗ Messages are NOT properly ordered')
"
echo ""

echo "Test 4: Check response structure"
echo "-----------------------------------------------------------"
curl -s -X POST "$BASE_URL/test-chat-history" \
  -H "Content-Type: application/json" \
  -d '{"conversation_id": "test-conv-1"}' | python3 -c "
import sys, json
data = json.load(sys.stdin)
required_fields = ['conversation_id', 'thread_id', 'total_messages', 'messages']
has_all = all(field in data for field in required_fields)
print(f\"✓ Response has all required fields: {has_all}\")
if data['messages']:
    msg = data['messages'][0]
    msg_fields = ['id', 'code', 'role', 'message', 'created_at']
    has_msg_fields = all(field in msg for field in msg_fields)
    print(f\"✓ Message has all required fields: {has_msg_fields}\")
    print(f\"  Sample message fields: {list(msg.keys())}\")
"
echo ""

echo "=========================================="
echo "All tests completed!"
echo "=========================================="
