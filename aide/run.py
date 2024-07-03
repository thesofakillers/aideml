import atexit
import logging
import shutil
from time import time
import signal

from aide.exceptions import SignalException

from . import backend

from .agent import Agent
from .interpreter import Interpreter
from .journal import Journal, Node
from omegaconf import OmegaConf
from rich.columns import Columns
from rich.console import Group
from rich.padding import Padding
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
)
from rich.text import Text
from rich.markdown import Markdown
from rich.status import Status
from rich.tree import Tree
from .utils.config import load_task_desc, prep_agent_workspace, save_run, load_cfg


def journal_to_rich_tree(journal: Journal):
    best_node = journal.get_best_node()

    def append_rec(node: Node, tree):
        if node.is_buggy:
            s = "[red]◍ bug"
        else:
            style = "bold " if node is best_node else ""

            if node is best_node:
                s = f"[{style}green]● {node.metric.value:.3f} (best)"
            else:
                s = f"[{style}green]● {node.metric.value:.3f}"

        subtree = tree.add(s)
        for child in node.children:
            append_rec(child, subtree)

    tree = Tree("[bold blue]Solution tree")
    for n in journal.draft_nodes:
        append_rec(n, tree)
    return tree


def journal_to_string_tree(journal: Journal) -> str:
    best_node = journal.get_best_node()
    tree_str = "Solution tree\n"

    def append_rec(node: Node, level: int):
        nonlocal tree_str
        indent = "  " * level
        if node.is_buggy:
            s = f"{indent}◍ bug (ID: {node.id})\n"
        else:
            # support for multiple markers; atm only "best" is supported
            markers = []
            if node is best_node:
                markers.append("best")
            marker_str = " & ".join(markers)
            if marker_str:
                s = f"{indent}● {node.metric.value:.3f} ({marker_str}) (ID: {node.id})\n"
            else:
                s = f"{indent}● {node.metric.value:.3f} (ID: {node.id})\n"
        tree_str += s
        for child in node.children:
            append_rec(child, level + 1)

    for n in journal.draft_nodes:
        append_rec(n, 0)

    return tree_str


def timeout_handler(signum, frame):
    """
    Raises a SignalException when a signal is received
    """
    raise SignalException("Execution timed out")


# when a signal.SIGALRM signal is received, raise a SignalException
signal.signal(signal.SIGALRM, timeout_handler)


def run():
    cfg = load_cfg()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper()),
        format="[%(asctime)s] %(levelname)s: %(message)s",
    )
    # dont want info logs from httpx
    httpx_logger: logging.Logger = logging.getLogger("httpx")
    httpx_logger.setLevel(logging.WARNING)

    logger = logging.getLogger("aide")
    logger.info(f'Starting run "{cfg.exp_name}"')

    task_desc = load_task_desc(cfg)
    task_desc_str = backend.compile_prompt_to_md(task_desc)

    with Status("Preparing agent workspace (copying and extracting files) ..."):
        prep_agent_workspace(cfg)

    def cleanup():
        if global_step == 0:
            shutil.rmtree(cfg.workspace_dir)

    atexit.register(cleanup)

    journal = Journal()
    agent = Agent(
        task_desc=task_desc,
        cfg=cfg,
        journal=journal,
    )

    # send a SIGALRM in acfg.time_limit seconds. This will terminate the run
    signal.alarm(agent.acfg.time_limit)
    try:
        interpreter = Interpreter(
            cfg.workspace_dir, **OmegaConf.to_container(cfg.exec)  # type: ignore
        )

        global_step = len(journal)
        prog = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=20),
            MofNCompleteColumn(),
            TimeRemainingColumn(),
        )
        status = Status("[green]Generating code...")
        prog.add_task("Progress:", total=cfg.agent.steps, completed=global_step)

        def exec_callback(*args, **kwargs):
            status.update("[magenta]Executing code...")
            res = interpreter.run(*args, **kwargs)
            status.update("[green]Generating code...")
            return res

        def generate_live():
            tree = journal_to_rich_tree(journal)
            prog.update(prog.task_ids[0], completed=global_step)

            file_paths = [
                f"Result visualization:\n[yellow]▶ {str((cfg.log_dir / 'tree_plot.html'))}",
                f"Agent workspace directory:\n[yellow]▶ {str(cfg.workspace_dir)}",
                f"Experiment log directory:\n[yellow]▶ {str(cfg.log_dir)}",
            ]
            left = Group(
                Panel(Text(task_desc_str.strip()), title="Task description"),
                prog,
                status,
            )
            right = tree
            wide = Group(*file_paths)

            return Panel(
                Group(
                    Padding(wide, (1, 1, 1, 1)),
                    Columns(
                        [Padding(left, (1, 2, 1, 1)), Padding(right, (1, 1, 1, 2))],
                        equal=True,
                    ),
                ),
                title=f'[b]AIDE is working on experiment: [bold green]"{cfg.exp_name}[/b]"',
                subtitle="Press [b]Ctrl+C[/b] to stop the run",
            )

        while global_step < cfg.agent.steps:
            agent.step(exec_callback=exec_callback)
            save_run(cfg, journal)
            global_step = len(journal)
        interpreter.cleanup_session()

        logger.info(journal_to_string_tree(journal))
    except SignalException as e:
        logger.info("Execution timed out")
    finally:
        save_run(cfg, journal)
        signal.alarm(0)


if __name__ == "__main__":
    run()
