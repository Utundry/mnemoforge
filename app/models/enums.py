from enum import Enum


class MemoryType(str, Enum):
    fact = "fact"
    preference = "preference"
    experience = "experience"
    task = "task"
    context = "context"
    profile = "profile"       # user/agent profile attributes (name, role, skills)
    procedural = "procedural"  # how-to knowledge, processes, algorithms
