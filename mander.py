from dataclasses import dataclass
import os
import subprocess
import argparse
import time
import logging
import re

logging.basicConfig(format='MANDER:%(levelname)s:%(message)s', level=logging.INFO)


RENDER_OUTPUT_BASE_DIR = 'C:\\blender_files\\renders'
SCRIPT_START_TIMESTAMP = time.strftime('%Y%m%d-%H%M%S')
# TODO: make platform-agnostic (use os.path more)
# TODO: instead of waiting on Blender Proc, start async and continuously monitor Blender process. forward kill events

#####################
# Manager Arguments #
#####################
parser = argparse.ArgumentParser(
    description='MANage RenDERing an animation. If Blender crashes, restart where it left off.')
parser.add_argument('project_file', help='path to the Blender project file (.blend)', type=str)
parser.add_argument('--max_retry', help='Maximum number of times to retry rendering  (default: %(default)s)',
                    type=int, default=10)
parser.add_argument('--resume',
                    help='Resume rendering the project using this directory instead of creating a new one  (Optional)',
                    type=str)


@dataclass
class BlenderCmd:
    project_file_path: str
    frame_output_path: str
    start_frame: int
    end_frame: int
    animate: bool

    @property
    def command_line(self) -> list[str]:
        blender_cmd = ["blender", "-b", str(self.project_file_path)]
        blender_cmd += ["--frame-start", str(self.start_frame)]
        blender_cmd += ["--frame-end", str(self.end_frame)]
        blender_cmd += ["--render-output", str(self.frame_output_path)]
        if self.animate:
            blender_cmd.append("-a")
        return blender_cmd


def run(blender_cmd: BlenderCmd,
        max_retries: int = 10,
        resume: bool = False):
    exit_code, num_retries = None, 0
    if resume:
        frames_rendered = get_frame_numbers_in_dir(blender_cmd.frame_output_path)
        last_frame = max(frames_rendered) if frames_rendered else 0
        blender_cmd.start_frame = last_frame + 1
    else:
        frames_rendered = []

    while exit_code not in [0, 1] and blender_cmd.end_frame not in frames_rendered and num_retries < max_retries:
        writer = BlenderLineInterpreter()
        logging.info(f"Blender Command: {' '.join(blender_cmd.command_line)}")
        proc = subprocess.Popen(blender_cmd.command_line, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        exit_code = proc.poll()
        while True:
            stdout = proc.stdout.readline()
            if stdout == b'' and exit_code is not None:
                break
            if stdout:
                writer.summarize(stdout.decode().strip())
            exit_code = proc.poll()

        frames_rendered = get_frame_numbers_in_dir(blender_cmd.frame_output_path)
        report_success(proc, blender_cmd)

        if exit_code != 0:
            num_retries += 1
            frames_rendered = get_frame_numbers_in_dir(blender_cmd.frame_output_path)
            last_frame = max(frames_rendered) if frames_rendered else 0
            logging.error(f'returncode: {proc.returncode}. Failed at frame: {last_frame}. '
                          f'retrying {max_retries - num_retries} more times...')
            if num_retries < max_retries:
                blender_cmd.start_frame = last_frame + 1


def report_success(completed_proc, blender_cmd: BlenderCmd):
    # print(completed_proc.stdout.decode())
    logging.info(f"Frames Rendered:{len(get_frame_numbers_in_dir(blender_cmd.frame_output_path))}")
    logging.debug("Manager completed with returncode: " + str(completed_proc.returncode))


def get_scene_frames(project_path) -> (int, int):
    bpy_script = ";".join([
        "import bpy",
        "print(f'start_frame={bpy.context.scene.frame_start}\\nend_frame={bpy.context.scene.frame_end}')"
    ])
    cmd = f'blender -b {project_path} --python-expr "{bpy_script}"'
    start_frame = None
    end_frame = None
    try:
        completed_proc = subprocess.run(cmd, check=True, capture_output=True)
        lines = completed_proc.stdout.decode().splitlines()
        for line in lines:
            if line.startswith("start_frame="):
                start_frame = int(line.split("start_frame=")[1])
            elif line.startswith("end_frame="):
                end_frame = int(line.split("end_frame=")[1])

        if None in [start_frame, end_frame]:
            raise KeyError("Unable to find start/end frames in blender output.")

        return start_frame, end_frame

    except (subprocess.CalledProcessError, KeyError) as e:
        logging.error(f'MANAGER QUIT. UNABLE TO GET FRAMES ON PROJECT: {project_path} \n {e}')
        exit(1)


def get_frame_numbers_in_dir(directory_path: str) -> list[int]:
    if not os.path.isdir(directory_path):
        return []

    filenames = [f for f in os.listdir(directory_path) if os.path.isfile(os.path.join(directory_path, f))]
    frames = [int(f.rsplit('.', maxsplit=1)[0]) for f in filenames]
    return frames


def new_frame_output_dir(project_file_path: str):
    name = project_file_path.split("\\")[-1].split(".blend")[0]
    project_render_dir = '_'.join([name, SCRIPT_START_TIMESTAMP])
    return os.path.join(RENDER_OUTPUT_BASE_DIR, project_render_dir)


class BlenderLineInterpreter:
    saved_expr = re.compile(r"Saved: '(.+)'")
    time_expr = re.compile(r"Time: (\d+:?\d+:\d+\.\d+) \(Saving: (\d+:?\d+:\d+\.\d+)\)")

    def __init__(self, status_message_frequency: int = 30):
        self.status_msg_freq = status_message_frequency  # max freq of frame-render status messages. still alive msg.
        self.last_report_time = time.time()

        self.saved_filename = None
        self.render_time = None

    def summarize(self, line: str):
        logging.debug(line)

        if line.startswith('Saved:'):
            try:
                self.saved_filename = self.saved_expr.match(line).group(1)
            except (AttributeError, IndexError):
                self.saved_filename = f"Unrecognized Saved File Path: {line}"  # probably never hit this

        elif line.startswith("Fra:") and (current_time := time.time()) - self.last_report_time > self.status_msg_freq:
            print(line)
            self.last_report_time = current_time

        elif line.startswith('Time:'):
            try:
                self.render_time = self.time_expr.match(line).group(1)
            except (AttributeError, IndexError):
                self.render_time = f"Unrecognized Render Time: {line}"  # probably never hit this

            # "Time: ..." Indicates final log message for a frame. Print cached stuff.
            print(f"{self.saved_filename}: {self.render_time}")
            self.last_report_time = time.time()


if __name__ == '__main__':
    args = parser.parse_args()
    project_file = os.path.abspath(args.project_file)
    frame_output_dir = os.path.abspath(args.resume) \
        if args.resume \
        else new_frame_output_dir(project_file)
    start, end = get_scene_frames(project_file)
    managed_cmd = BlenderCmd(
        project_file_path=project_file,
        frame_output_path=frame_output_dir if frame_output_dir.endswith('\\') else frame_output_dir + '\\',
        start_frame=start,
        end_frame=end,
        animate=True
    )

    run(managed_cmd, max_retries=args.max_retry, resume=bool(args.resume))
