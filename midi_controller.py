import mido
import sys
import asyncio
import websockets
import json
from time import sleep
import logging
import os
from typing import Optional
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.table import Table
from rich import print as rprint
import typer

# Initialize Rich console and CLI app
console = Console()
app = typer.Typer()

# Constants
DEVICE_SAVE_FILE = 'last_device.txt'
CONTROL_SAVE_FILE = 'last_control.txt'
VERSION = "1.0.0"

# Global state
current_value = 0
previous_value = 0
clients = set()
watching_control = None

def display_header():
    """Display application header"""
    console.print(Panel.fit(
        f"[bold blue]MIDI Controller Monitor v{VERSION}[/]",
        border_style="blue"
    ))

def display_devices_table(devices: list) -> None:
    """Display available MIDI devices in a formatted table"""
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("#", style="dim", width=4)
    table.add_column("Device Name", style="cyan")
    
    for idx, device in enumerate(devices, 1):
        table.add_row(str(idx), device)
    
    console.print(table)

def select_midi_device() -> Optional[str]:
    """Interactive MIDI device selection with rich UI"""
    available_devices = mido.get_input_names()
    
    if not available_devices:
        console.print("[red]No MIDI input devices found![/]")
        return None
    
    # Check for saved device
    saved_device = load_saved_device()
    if saved_device and saved_device in available_devices:
        if Confirm.ask(
            f"\n[green]Found previously saved device:[/] [blue]{saved_device}[/]\nUse this device?",
            default=True
        ):
            return saved_device
    
    console.print("\n[bold]Available MIDI Input Devices:[/]")
    display_devices_table(available_devices)
    
    while True:
        try:
            selection = Prompt.ask(
                "\nSelect device number",
                show_default=False,
                default="1"
            )
            
            if selection.lower() == 'q':
                raise typer.Exit()
            
            device_idx = int(selection) - 1
            if 0 <= device_idx < len(available_devices):
                selected_device = available_devices[device_idx]
                save_selected_device(selected_device)
                return selected_device
            else:
                console.print("[red]Invalid selection. Please try again.[/]")
        except ValueError:
            console.print("[red]Please enter a valid number.[/]")

async def midi_monitor(device_name: str) -> None:
    """Monitor MIDI input from specified device"""
    global current_value, previous_value, watching_control
    
    try:
        console.print(f"\n[green]Monitoring MIDI input from:[/] [blue]{device_name}[/]")
        
        # Learn control number if not set
        if watching_control is None:
            watching_control = await learn_control(device_name)
            if watching_control is None:
                console.print("[red]Failed to learn control number. Exiting...[/]")
                return
            console.print(f"[green]Watching control number:[/] [blue]{watching_control}[/]")
        
        console.print("[dim]Watching crossfader values...[/]")
        
        with mido.open_input(device_name) as inport:
            with console.status("") as status:
                while True:
                    for message in inport.iter_pending():
                        if message.type == 'control_change' and message.control == watching_control:
                            current_value = scale_value(message.value)
                            if current_value != previous_value:
                                status.update(f"Crossfader: [cyan]{current_value}%[/]")
                                logging.info(f"Crossfader: {current_value}%")
                                await broadcast_value()
                                previous_value = current_value
                    await asyncio.sleep(0.01)

    except Exception as e:
        console.print(f"\n[red]An error occurred:[/] {str(e)}")

async def websocket_handler(websocket):
    """Handle websocket connections"""
    try:
        # Register client
        clients.add(websocket)
        print("Client connected")
        
        # Send initial value
        await websocket.send(json.dumps({"crossfader": current_value}))
        
        # Keep connection alive and handle incoming messages
        while True:
            try:
                # Wait for a message with a timeout
                await asyncio.wait_for(websocket.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                try:
                    # Send a ping to keep the connection alive
                    pong_waiter = await websocket.ping()
                    await asyncio.wait_for(pong_waiter, timeout=1.0)
                except asyncio.TimeoutError:
                    # No pong received, connection is dead
                    raise websockets.exceptions.ConnectionClosed(
                        1005, "Connection timed out"
                    )
            except websockets.exceptions.ConnectionClosed:
                raise
            
    except websockets.exceptions.ConnectionClosed as e:
        print(f"Client disconnected with code {e.code}: {e.reason}")
    except Exception as e:
        print(f"Unexpected websocket error: {str(e)}")
    finally:
        clients.remove(websocket)

async def broadcast_value():
    """Broadcast value to all connected clients"""
    if not clients:  # Skip if no clients
        return
        
    message = json.dumps({"crossfader": current_value})
    disconnected_clients = set()
    
    for client in clients:
        try:
            await client.send(message)
        except websockets.exceptions.ConnectionClosed:
            disconnected_clients.add(client)
        except Exception as e:
            print(f"Error broadcasting to client: {str(e)}")
            disconnected_clients.add(client)
    
    # Remove disconnected clients
    clients.difference_update(disconnected_clients)

def scale_value(value, in_min=0, in_max=127, out_min=0, out_max=100):
    """Scale a value from one range to another"""
    return round((value - in_min) * (out_max - out_min) / (in_max - in_min) + out_min)

# Add new functions for device handling
def save_selected_device(device_name):
    """Save the selected device name to a file"""
    try:
        with open(DEVICE_SAVE_FILE, 'w') as f:
            f.write(device_name)
    except Exception as e:
        print(f"Could not save device selection: {e}")

def load_saved_device():
    """Load the previously selected device name from file"""
    try:
        if os.path.exists(DEVICE_SAVE_FILE):
            with open(DEVICE_SAVE_FILE, 'r') as f:
                return f.read().strip()
    except Exception as e:
        print(f"Could not load saved device: {e}")
    return None

async def learn_control(device_name: str) -> Optional[int]:
    """Interactive control number learning mode"""
    # Check for saved control number
    saved_control = load_control_number()
    if saved_control is not None:
        if Confirm.ask(
            f"\n[green]Found previously saved control number:[/] [blue]{saved_control}[/]\nUse this control?",
            default=True
        ):
            return saved_control
    
    console.print("\n[yellow]Entering MIDI Control Learn Mode[/]")
    console.print("[dim]1. Move ONLY the crossfader fully left to right[/]")
    console.print("[dim]2. When done, we'll show you the detected controls[/]")
    console.print("[dim]3. Press Ctrl+C when you're done moving the crossfader[/]")
    
    try:
        detected_controls = {}  # Store control numbers and their value ranges
        
        with mido.open_input(device_name) as inport:
            with console.status("[bold yellow]Monitoring crossfader movement...[/]") as status:
                try:
                    # Monitor for 5 seconds or until Ctrl+C
                    for _ in range(500):  # 5 seconds (0.01s * 500)
                        for message in inport.iter_pending():
                            if message.type == 'control_change':
                                control = message.control
                                value = message.value
                                
                                if control not in detected_controls:
                                    detected_controls[control] = {
                                        'min': value,
                                        'max': value,
                                        'changes': 1
                                    }
                                else:
                                    detected_controls[control]['min'] = min(detected_controls[control]['min'], value)
                                    detected_controls[control]['max'] = max(detected_controls[control]['max'], value)
                                    detected_controls[control]['changes'] += 1
                                
                                status.update(f"Monitoring... Found {len(detected_controls)} controls")
                        
                        await asyncio.sleep(0.01)
                except KeyboardInterrupt:
                    pass  # Allow early exit with Ctrl+C
        
        # Filter controls with significant movement
        significant_controls = {
            ctrl: data for ctrl, data in detected_controls.items()
            if (data['max'] - data['min'] > 20 and data['changes'] > 5)
        }
        
        if not significant_controls:
            console.print("[red]No significant control movements detected. Please try again.[/]")
            return None
        
        # Display table of detected controls
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("Control #", style="cyan")
        table.add_column("Range", style="green")
        table.add_column("# Changes", style="yellow")
        
        for ctrl, data in significant_controls.items():
            table.add_row(
                str(ctrl),
                f"{data['min']} - {data['max']}",
                str(data['changes'])
            )
        
        console.print("\n[bold]Detected Controls with Significant Movement:[/]")
        console.print(table)
        
        # Let user select the correct control
        while True:
            selection = Prompt.ask(
                "\nEnter the control number for your crossfader",
                choices=[str(ctrl) for ctrl in significant_controls.keys()],
                show_choices=False
            )
            
            try:
                selected_control = int(selection)
                save_control_number(selected_control)  # Save the selected control
                return selected_control
            except ValueError:
                console.print("[red]Please enter a valid control number[/]")
                
    except Exception as e:
        console.print(f"[red]Error in learn mode:[/] {str(e)}")
        return None

# Add new function to save control number
def save_control_number(control: int):
    """Save the selected control number to a file"""
    try:
        with open(CONTROL_SAVE_FILE, 'w') as f:
            f.write(str(control))
    except Exception as e:
        console.print(f"[red]Could not save control selection: {e}[/]")

# Add new function to load control number
def load_control_number() -> Optional[int]:
    """Load the previously selected control number from file"""
    try:
        if os.path.exists(CONTROL_SAVE_FILE):
            with open(CONTROL_SAVE_FILE, 'r') as f:
                return int(f.read().strip())
    except Exception as e:
        console.print(f"[red]Could not load saved control: {e}[/]")
    return None

@app.command()
def main(device: str = typer.Argument(None, help="MIDI device name")):
    """
    MIDI Controller Monitor - Track and broadcast MIDI controller values via WebSocket
    """
    try:
        display_header()
        
        async def run_server():
            # Start websocket server
            server = await websockets.serve(websocket_handler, "localhost", 8765)
            console.print(
                "[green]WebSocket server started at[/] [blue]ws://localhost:8765[/]"
            )
            
            # Get device selection
            device_name = device or select_midi_device()
            if not device_name:
                console.print("[red]No MIDI device selected. Exiting...[/]")
                return
            
            # Run both tasks concurrently
            await asyncio.gather(
                server.serve_forever(),
                midi_monitor(device_name)
            )
        
        asyncio.run(run_server())
        
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down...[/]")
    except Exception as e:
        console.print(f"[red]Fatal error:[/] {str(e)}")
        raise typer.Exit(1)

if __name__ == "__main__":
    app() 