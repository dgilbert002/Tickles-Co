"""
JarvAIs — Context Snapshot Service
Ensures no conversation context is ever lost by periodically compressing
conversation state into searchable, vectorized snapshots.

Triggers: turn count, token count, conversation end, manual, context overflow.
Snapshots are stored in MySQL + vectorized in the conversations collection.
"""

import json
import logging
import uuid
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger("jarvais.context_snapshot")

SNAPSHOT_SYSTEM_PROMPT = """You are a conversation archiver for JarvAIs, an AI trading intelligence company.
Your job is to compress a section of conversation into a factual, lossless summary.

RULES:
- Preserve EVERY decision made and WHO made it
- Preserve EVERY action item, open question, and disagreement
- Preserve exact numbers, prices, symbols, dates mentioned
- Preserve each participant's current position/opinion
- Note any RAG queries agents made and what they found
- Note any delegations between agents
- Use direct quotes for critical statements
- If someone changed their mind, note both the old and new position
- NEVER omit information — compress, don't delete
- Format as structured sections: Decisions, Positions, Open Items, Key Facts"""


class ContextSnapshotService:
    """Creates and manages conversation context snapshots."""

    def __init__(self, db=None, model_interface=None):
        self._db = db
        self._model = model_interface

    def _get_db(self):
        if self._db:
            return self._db
        from db.database import get_db
        self._db = get_db()
        return self._db

    def _get_model(self):
        if self._model:
            return self._model
        from core.model_interface import ModelInterface
        self._model = ModelInterface()
        return self._model

    def _get_config(self, key: str, default: str) -> str:
        db = self._get_db()
        try:
            row = db.fetch_one(
                "SELECT config_value FROM system_config WHERE config_key = %s", (key,)
            )
            return row["config_value"] if row else default
        except Exception:
            return default

    @property
    def snapshot_turn_interval(self) -> int:
        return int(self._get_config("snapshot_interval_turns", "15"))

    @property
    def snapshot_token_threshold(self) -> int:
        return int(self._get_config("snapshot_interval_tokens", "20000"))

    # ─────────────────────────────────────────────────────────────
    # Check if snapshot is needed
    # ─────────────────────────────────────────────────────────────

    def should_snapshot(self, conversation_id: int) -> Optional[str]:
        """
        Check if a conversation needs a snapshot.
        Returns the trigger reason or None.
        """
        db = self._get_db()
        conv = db.fetch_one(
            "SELECT turn_count, total_tokens FROM agent_conversations WHERE id = %s",
            (conversation_id,)
        )
        if not conv:
            return None

        last_snap = db.fetch_one(
            "SELECT to_turn FROM context_snapshots WHERE conversation_id = %s ORDER BY to_turn DESC LIMIT 1",
            (conversation_id,)
        )
        last_turn = last_snap["to_turn"] if last_snap else 0
        turns_since = conv["turn_count"] - last_turn

        if turns_since >= self.snapshot_turn_interval:
            return "periodic"

        last_snap_tokens = db.fetch_one("""
            SELECT COALESCE(SUM(token_count), 0) as tok
            FROM conversation_turns
            WHERE conversation_id = %s AND turn_number > %s
        """, (conversation_id, last_turn))
        tokens_since = last_snap_tokens["tok"] if last_snap_tokens else 0

        if tokens_since >= self.snapshot_token_threshold:
            return "context_overflow"

        return None

    # ─────────────────────────────────────────────────────────────
    # Create snapshot
    # ─────────────────────────────────────────────────────────────

    def create_snapshot(self, conversation_id: int,
                        snapshot_type: str = "periodic",
                        force: bool = False) -> Optional[int]:
        """
        Create a context snapshot for a conversation.
        Returns the snapshot ID or None if not needed.
        """
        db = self._get_db()

        conv = db.fetch_one(
            "SELECT id, chain_id, topic, participants FROM agent_conversations WHERE id = %s",
            (conversation_id,)
        )
        if not conv:
            logger.warning(f"[Snapshot] Conversation {conversation_id} not found")
            return None

        last_snap = db.fetch_one(
            "SELECT to_turn FROM context_snapshots WHERE conversation_id = %s ORDER BY to_turn DESC LIMIT 1",
            (conversation_id,)
        )
        from_turn = (last_snap["to_turn"] + 1) if last_snap else 1

        turns = db.fetch_all("""
            SELECT turn_number, speaker_id, message_content, reasoning_chain,
                   context_used, token_count, created_at
            FROM conversation_turns
            WHERE conversation_id = %s AND turn_number >= %s
            ORDER BY turn_number ASC
        """, (conversation_id, from_turn))

        if not turns and not force:
            return None

        if not turns:
            turns = []

        to_turn = turns[-1]["turn_number"] if turns else from_turn

        # Build conversation text for the AI compressor
        conv_text = self._format_turns_for_snapshot(turns, conv.get("topic", ""))

        # Call cheap model to compress
        summary_text, key_decisions, key_entities, positions, token_count, cost = (
            self._generate_snapshot(conv_text, conv.get("participants"))
        )

        # Store snapshot
        snapshot_id = self._store_snapshot(
            db, conversation_id, conv.get("chain_id"),
            snapshot_type, from_turn, to_turn,
            summary_text, key_decisions, key_entities, positions,
            token_count, cost
        )

        # Vectorize the snapshot
        self._vectorize_snapshot(db, snapshot_id, conv, summary_text, key_entities, key_decisions)

        # Record lineage: snapshot <- turns
        self._record_snapshot_lineage(db, snapshot_id, conversation_id, from_turn, to_turn)

        logger.info(
            f"[Snapshot] Created {snapshot_type} snapshot #{snapshot_id} "
            f"for conv {conversation_id} (turns {from_turn}-{to_turn})"
        )
        return snapshot_id

    def _format_turns_for_snapshot(self, turns: List[Dict], topic: str) -> str:
        lines = []
        if topic:
            lines.append(f"TOPIC: {topic}")
            lines.append("")
        for t in turns:
            ts = str(t.get("created_at", ""))
            speaker = t["speaker_id"]
            content = t["message_content"]
            lines.append(f"[Turn {t['turn_number']}] {speaker} ({ts}):")
            lines.append(content)
            if t.get("reasoning_chain"):
                rc = t["reasoning_chain"]
                if isinstance(rc, str):
                    rc = json.loads(rc)
                for step in rc:
                    lines.append(f"  [Internal-{step.get('step', '?')}]: {step.get('thought', '')}")
            if t.get("context_used"):
                cu = t["context_used"]
                if isinstance(cu, str):
                    cu = json.loads(cu)
                for c in cu:
                    lines.append(f"  [RAG Query]: {c.get('query', '')} -> {c.get('results_summary', '')}")
            lines.append("")
        return "\n".join(lines)

    def _generate_snapshot(self, conv_text: str, participants) -> tuple:
        """Call a cheap AI model to compress the conversation section."""
        if not conv_text.strip():
            return ("No content to snapshot.", None, None, None, 0, 0.0)

        model = self._get_model()
        user_prompt = (
            "Compress the following conversation section into a structured, lossless summary.\n\n"
            f"CONVERSATION:\n{conv_text[:50000]}"
        )

        try:
            result = model.query(
                role="analyst",
                system_prompt=SNAPSHOT_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                context="context_snapshot",
                model_role="primary",
                max_tokens=2000,
                temperature=0.1,
            )
            summary = result.content if result and result.content else conv_text[:5000]
            tokens = (result.token_count_input or 0) + (result.token_count_output or 0) if result else 0
            cost = result.cost if result else 0.0
        except Exception as e:
            logger.warning(f"[Snapshot] AI compression failed, using raw: {e}")
            summary = conv_text[:5000]
            tokens = 0
            cost = 0.0

        key_decisions = self._extract_json_section(summary, "decisions")
        key_entities = self._extract_entities(conv_text)
        positions = self._extract_json_section(summary, "positions")

        return (summary, key_decisions, key_entities, positions, tokens, cost)

    def _extract_json_section(self, text: str, section: str) -> Optional[list]:
        """Best-effort extraction of structured sections from the summary."""
        try:
            lower = text.lower()
            idx = lower.find(section.lower())
            if idx == -1:
                return None
            chunk = text[idx:idx + 2000]
            lines = [l.strip() for l in chunk.split("\n") if l.strip() and l.strip() != section.title() + ":"]
            items = []
            for l in lines[1:]:
                if l.startswith("-") or l.startswith("*") or l[0].isdigit():
                    items.append(l.lstrip("-*0123456789. "))
                elif not l[0].isalpha():
                    break
                else:
                    break
            return items if items else None
        except Exception:
            return None

    def _extract_entities(self, text: str) -> Optional[dict]:
        """Extract symbols, agent names from conversation text."""
        import re
        symbols = list(set(re.findall(r'\b((?:XAU|BTC|ETH|EUR|GBP|USD|NAS|SPX)\w{0,6})\b', text.upper())))
        agents = list(set(re.findall(r'\b(atlas|echo|quant|apex|mentor|signal|tracker|warren|elon|anvil|vault|pixel|forge|cipher|justice|ledger|curiosity|vox|lens|scribe|reel|geo|macro|billnye)\b', text.lower())))
        if not symbols and not agents:
            return None
        return {"symbols": symbols[:20], "agents": agents[:20]}

    def _store_snapshot(self, db, conversation_id, chain_id, snapshot_type,
                        from_turn, to_turn, summary, decisions, entities,
                        positions, tokens, cost) -> int:
        snapshot_id = db.execute_returning_id("""
            INSERT INTO context_snapshots
                (conversation_id, chain_id, snapshot_type, from_turn, to_turn,
                 summary_text, key_decisions, key_entities, participant_positions,
                 token_count, model_used, cost_usd)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            conversation_id, chain_id, snapshot_type, from_turn, to_turn,
            summary,
            json.dumps(decisions) if decisions else None,
            json.dumps(entities) if entities else None,
            json.dumps(positions) if positions else None,
            tokens, "nano", cost or 0,
        ))
        return snapshot_id or 0

    def _vectorize_snapshot(self, db, snapshot_id, conv, summary, entities, decisions):
        try:
            from services.vectorization_worker import queue_for_vectorization
            topic = conv.get("topic", "")
            vec_text = f"SNAPSHOT [{topic}]: {summary}"[:30000]
            meta = {
                "conversation_id": conv["id"],
                "chain_id": conv.get("chain_id", ""),
                "snapshot_type": "periodic",
                "content_type": "context_snapshot",
            }
            if entities:
                meta["symbols"] = entities.get("symbols", [])
                meta["agents"] = entities.get("agents", [])
            queue_for_vectorization(db, "context_snapshots", snapshot_id,
                                    "conversations", vec_text, meta)
        except Exception as e:
            logger.debug(f"[Snapshot] Vectorization queue error: {e}")

    def _record_snapshot_lineage(self, db, snapshot_id, conversation_id, from_turn, to_turn):
        try:
            from services.vectorization_worker import record_lineage
            turn_ids = db.fetch_all("""
                SELECT id FROM conversation_turns
                WHERE conversation_id = %s AND turn_number BETWEEN %s AND %s
            """, (conversation_id, from_turn, to_turn))
            if turn_ids:
                record_lineage(db, "context_snapshots", snapshot_id,
                               [("conversation_turns", r["id"]) for r in turn_ids],
                               "summarized_from")
        except Exception as e:
            logger.debug(f"[Snapshot] Lineage error: {e}")

    # ─────────────────────────────────────────────────────────────
    # Context loading for chain continuation
    # ─────────────────────────────────────────────────────────────

    def load_chain_context(self, chain_id: str, max_tokens: int = 4000) -> str:
        """
        Load conversation context for chain continuation.
        Returns compressed context from the most recent snapshot +
        any turns after it, ready to inject as system context.
        """
        db = self._get_db()

        latest_snap = db.fetch_one("""
            SELECT cs.summary_text, cs.to_turn, cs.conversation_id,
                   ac.topic, cs.key_decisions, cs.participant_positions
            FROM context_snapshots cs
            JOIN agent_conversations ac ON ac.id = cs.conversation_id
            WHERE cs.chain_id = %s
            ORDER BY cs.created_at DESC
            LIMIT 1
        """, (chain_id,))

        if not latest_snap:
            return ""

        parts = []
        parts.append(f"=== PREVIOUS CONTEXT (Topic: {latest_snap.get('topic', 'unknown')}) ===")
        parts.append(latest_snap["summary_text"])

        # Add any turns after the snapshot
        recent_turns = db.fetch_all("""
            SELECT ct.turn_number, ct.speaker_id, ct.message_content
            FROM conversation_turns ct
            JOIN agent_conversations ac ON ac.id = ct.conversation_id
            WHERE ac.chain_id = %s AND ct.turn_number > %s
            ORDER BY ct.created_at ASC
            LIMIT 50
        """, (chain_id, latest_snap["to_turn"]))

        if recent_turns:
            parts.append("\n=== RECENT (since last snapshot) ===")
            for t in recent_turns:
                parts.append(f"{t['speaker_id']}: {t['message_content']}")

        context = "\n".join(parts)
        # Rough token estimate: 1 token ~= 4 chars
        if len(context) > max_tokens * 4:
            context = context[:max_tokens * 4] + "\n[...truncated, see snapshots for full history]"

        return context

    def load_full_chain_context(self, chain_id: str) -> str:
        """
        Load ALL snapshots for a chain in chronological order.
        For building comprehensive agent context or audit trails.
        """
        db = self._get_db()
        snaps = db.fetch_all("""
            SELECT cs.summary_text, cs.from_turn, cs.to_turn,
                   cs.snapshot_type, cs.created_at, ac.topic
            FROM context_snapshots cs
            JOIN agent_conversations ac ON ac.id = cs.conversation_id
            WHERE cs.chain_id = %s
            ORDER BY cs.created_at ASC
        """, (chain_id,))

        if not snaps:
            return ""

        parts = []
        for s in snaps:
            parts.append(f"--- Snapshot (turns {s['from_turn']}-{s['to_turn']}, {s['created_at']}) ---")
            parts.append(s["summary_text"])
            parts.append("")

        return "\n".join(parts)

    # ─────────────────────────────────────────────────────────────
    # Conversation lifecycle helpers
    # ─────────────────────────────────────────────────────────────

    def on_conversation_end(self, conversation_id: int) -> Optional[int]:
        """Create an end-of-conversation snapshot."""
        return self.create_snapshot(conversation_id, "end_of_conversation", force=True)

    def on_turn_added(self, conversation_id: int, turn_token_count: int = 0) -> Optional[int]:
        """
        Called after every turn. Checks if a snapshot is needed.
        Updates conversation counters and triggers snapshot if threshold met.
        """
        db = self._get_db()
        db.execute("""
            UPDATE agent_conversations
            SET turn_count = turn_count + 1,
                total_tokens = total_tokens + %s
            WHERE id = %s
        """, (turn_token_count, conversation_id))

        trigger = self.should_snapshot(conversation_id)
        if trigger:
            return self.create_snapshot(conversation_id, trigger)
        return None

    def start_conversation(self, conversation_type: str, topic: str,
                           participants: list, initiator: str = "human_ceo",
                           chain_id: Optional[str] = None,
                           parent_conversation_id: Optional[int] = None) -> int:
        """
        Start a new conversation, optionally linked to a chain.
        If chain_id is provided and a previous snapshot exists,
        the snapshot ID is recorded for context bootstrapping.
        """
        db = self._get_db()

        if not chain_id:
            chain_id = str(uuid.uuid4())[:12]

        context_snapshot_id = None
        if chain_id:
            snap = db.fetch_one("""
                SELECT cs.id FROM context_snapshots cs
                JOIN agent_conversations ac ON ac.id = cs.conversation_id
                WHERE ac.chain_id = %s
                ORDER BY cs.created_at DESC LIMIT 1
            """, (chain_id,))
            if snap:
                context_snapshot_id = snap["id"]

        conv_id = db.execute_returning_id("""
            INSERT INTO agent_conversations
                (conversation_type, topic, participants, initiator,
                 chain_id, parent_conversation_id, context_snapshot_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            conversation_type, topic, json.dumps(participants),
            initiator, chain_id, parent_conversation_id, context_snapshot_id,
        ))

        logger.info(
            f"[Conversation] Started #{conv_id} type={conversation_type} "
            f"chain={chain_id} topic={topic[:60]}"
        )
        return conv_id

    def add_turn(self, conversation_id: int, speaker_id: str,
                 message_content: str, reasoning_chain: Optional[list] = None,
                 context_used: Optional[list] = None,
                 citations: Optional[list] = None,
                 tool_calls: Optional[list] = None,
                 delegation_request = None,
                 parent_turn_id: Optional[int] = None,
                 token_count: int = 0) -> int:
        """
        Add a turn to a conversation. Queues for vectorization.
        Triggers snapshot check. Returns turn ID.
        """
        db = self._get_db()

        conv = db.fetch_one(
            "SELECT turn_count, chain_id, topic, participants FROM agent_conversations WHERE id = %s",
            (conversation_id,)
        )
        turn_number = (conv["turn_count"] if conv else 0) + 1

        turn_id = db.execute_returning_id("""
            INSERT INTO conversation_turns
                (conversation_id, turn_number, speaker_id, message_content,
                 tool_calls, citations, delegation_request,
                 parent_turn_id, reasoning_chain, context_used, token_count)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            conversation_id, turn_number, speaker_id, message_content,
            json.dumps(tool_calls) if tool_calls else None,
            json.dumps(citations) if citations else None,
            json.dumps(delegation_request) if delegation_request else None,
            parent_turn_id,
            json.dumps(reasoning_chain) if reasoning_chain else None,
            json.dumps(context_used) if context_used else None,
            token_count,
        ))

        # Vectorize this turn
        self._vectorize_turn(db, turn_id, conversation_id, conv, turn_number,
                             speaker_id, message_content)

        # Check snapshot trigger
        self.on_turn_added(conversation_id, token_count)

        return turn_id

    def _vectorize_turn(self, db, turn_id, conversation_id, conv, turn_number,
                        speaker_id, message_content):
        try:
            from services.vectorization_worker import queue_for_vectorization
            vec_text = f"{speaker_id}: {message_content}"[:30000]
            meta = {
                "conversation_id": conversation_id,
                "chain_id": conv.get("chain_id", "") if conv else "",
                "turn_number": turn_number,
                "speaker_id": speaker_id,
                "topic": conv.get("topic", "") if conv else "",
                "content_type": "conversation_turn",
            }
            queue_for_vectorization(db, "conversation_turns", turn_id,
                                    "conversations", vec_text, meta)
        except Exception as e:
            logger.debug(f"[Conversation] Turn vectorization error: {e}")
