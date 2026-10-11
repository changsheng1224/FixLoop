"""S1 schema. No migration of legacy linear runtime sessions is implied."""

SCHEMA_VERSION = 1
APPLICATION_ID = 0x464C5742  # FLWB

TABLES = (
    """CREATE TABLE conversations (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        project_path TEXT,
        base_snapshot_id TEXT,
        active_branch_id TEXT NOT NULL,
        view_node_id TEXT,
        revision INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        FOREIGN KEY (active_branch_id, id) REFERENCES branches(id, conversation_id)
            DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY (view_node_id, id) REFERENCES nodes(id, conversation_id),
        FOREIGN KEY (base_snapshot_id, id) REFERENCES snapshots(id, conversation_id)
    )""",
    """CREATE TABLE branches (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id),
        name TEXT NOT NULL,
        fork_node_id TEXT,
        head_node_id TEXT,
        created_at REAL NOT NULL,
        UNIQUE (id, conversation_id),
        FOREIGN KEY (fork_node_id, conversation_id) REFERENCES nodes(id, conversation_id),
        FOREIGN KEY (head_node_id, conversation_id) REFERENCES nodes(id, conversation_id)
    )""",
    """CREATE TABLE snapshots (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id),
        storage_key TEXT NOT NULL,
        manifest TEXT NOT NULL,
        created_at REAL NOT NULL,
        UNIQUE (id, conversation_id)
    )""",
    """CREATE TABLE nodes (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id),
        parent_id TEXT,
        kind TEXT NOT NULL CHECK (kind IN ('turn', 'local_checkpoint')),
        user_text TEXT NOT NULL,
        mode TEXT CHECK (mode IN ('chat', 'project_chat', 'repair')),
        input_snapshot_id TEXT,
        checkpoint_snapshot_id TEXT,
        created_at REAL NOT NULL,
        UNIQUE (id, conversation_id),
        CHECK ((kind = 'turn' AND mode IS NOT NULL AND checkpoint_snapshot_id IS NULL)
            OR (kind = 'local_checkpoint' AND mode IS NULL
                AND checkpoint_snapshot_id IS NOT NULL)),
        FOREIGN KEY (parent_id, conversation_id) REFERENCES nodes(id, conversation_id),
        FOREIGN KEY (input_snapshot_id, conversation_id) REFERENCES snapshots(id, conversation_id),
        FOREIGN KEY (checkpoint_snapshot_id, conversation_id)
            REFERENCES snapshots(id, conversation_id)
    )""",
    """CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id),
        node_id TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL CHECK (
            status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted')),
        answer TEXT NOT NULL DEFAULT '',
        error TEXT,
        artifacts TEXT NOT NULL DEFAULT '{}',
        output_snapshot_id TEXT,
        created_at REAL NOT NULL,
        started_at REAL,
        finished_at REAL,
        CHECK ((status IN ('queued', 'running') AND finished_at IS NULL)
            OR (status NOT IN ('queued', 'running') AND finished_at IS NOT NULL)),
        FOREIGN KEY (node_id, conversation_id) REFERENCES nodes(id, conversation_id),
        FOREIGN KEY (output_snapshot_id, conversation_id)
            REFERENCES snapshots(id, conversation_id)
    )""",
    "CREATE INDEX nodes_parent ON nodes(conversation_id, parent_id)",
    "CREATE INDEX branches_conversation ON branches(conversation_id)",
    "CREATE INDEX runs_conversation ON runs(conversation_id, status)",
    "CREATE INDEX snapshots_conversation ON snapshots(conversation_id)",
    """CREATE TRIGGER terminal_run_immutable BEFORE UPDATE ON runs
        WHEN OLD.status NOT IN ('queued', 'running')
        BEGIN SELECT RAISE(ABORT, 'terminal run is immutable'); END""",
    """CREATE TRIGGER runs_delete_immutable BEFORE DELETE ON runs
        BEGIN SELECT RAISE(ABORT, 'execution history is immutable'); END""",
)

IMMUTABLE_TABLES = ("nodes", "snapshots")
