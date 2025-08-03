import asyncio
import os
from quart import Quart, websocket
from quart_cors import cors
import google.genai as genai
from google.genai import types # Crucial for Content, Part, Blob
from dotenv import load_dotenv

load_dotenv()

GOOGLE_API_KEY = os.getenv("GEMINI_API_KEY")
if not GOOGLE_API_KEY:
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
    if not GOOGLE_API_KEY:
        raise ValueError("GEMINI_API_KEY (or GOOGLE_API_KEY) environment variable not set.")

gemini_client = genai.Client(api_key=GOOGLE_API_KEY)
print("Using Google AI SDK with genai.Client.")

GEMINI_MODEL_NAME = "gemini-2.0-flash-live-001"
INPUT_SAMPLE_RATE = 16000

app = Quart(__name__)
app = cors(app, allow_origin="*")

@app.websocket("/listen")
async def websocket_endpoint():
    print("Quart WebSocket: Connection accepted from client.")
    current_session_handle = None # Initialize session handle

    gemini_live_config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        session_resumption=types.SessionResumptionConfig(handle=current_session_handle),
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(),
        ),
        realtime_input_config=types.RealtimeInputConfig( # Added realtime_input_config for VAD
            automatic_activity_detection=types.AutomaticActivityDetection(
                disabled=False,
                start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH, # Changed to MEDIUM
                end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                prefix_padding_ms=20,
                silence_duration_ms=100,
            )
        )
    )

    try:
        async with gemini_client.aio.live.connect(
            model=GEMINI_MODEL_NAME,
            config=gemini_live_config # Pass the config object
        ) as session:
            print(f"Quart Backend: Gemini session connected for model {GEMINI_MODEL_NAME}.")
            active_processing = True

            async def handle_client_input_and_forward():
                nonlocal active_processing
                print("Quart Backend: Starting handle_client_input_and_forward task.")
                try:
                    while active_processing:
                        try:
                            client_data = await asyncio.wait_for(websocket.receive(), timeout=0.2) # Reverted to receive()

                            if isinstance(client_data, str):
                                message_text = client_data
                                print(f"Quart Backend: Received text from client: '{message_text}'")
                                prompt_for_gemini = message_text
                                if message_text == "SEND_TEST_AUDIO_PLEASE":
                                    prompt_for_gemini = "Hello Gemini, please say 'testing one two three'."
                                
                                print(f"Quart Backend: Sending text prompt to Gemini: '{prompt_for_gemini}'")
                                user_content_for_text = types.Content(
                                    role="user",
                                    parts=[types.Part(text=prompt_for_gemini)]
                                )
                                # For a text prompt that expects a full response, turn_complete might be true.
                                # However, in a live session, even text might be part of an ongoing exchange.
                                # The example shows sending turns without explicitly setting turn_complete on send_client_content.
                                # It might be part of the Content object itself if needed, or controlled by session.
                                await session.send_client_content(turns=user_content_for_text)
                                print(f"Quart Backend: Prompt '{prompt_for_gemini}' sent to Gemini.")
                            
                            elif isinstance(client_data, bytes):
                                audio_chunk = client_data
                                if audio_chunk:
                                    print(f"Quart Backend: Received mic audio chunk: {len(audio_chunk)} bytes")
                                    print(f"Quart Backend: Sending audio chunk ({len(audio_chunk)} bytes) to Gemini via send_realtime_input...")
                                    await session.send_realtime_input(
                                        audio=types.Blob(
                                            mime_type=f"audio/pcm;rate={INPUT_SAMPLE_RATE}", # Changed mime_type
                                            data=audio_chunk
                                        )
                                    )
                                    print(f"Quart Backend: Successfully sent mic audio to Gemini via send_realtime_input.")
                            else:
                                print(f"Quart Backend: Received unexpected data type from client: {type(client_data)}, content: {client_data[:100] if isinstance(client_data, bytes) else client_data}")

                        except asyncio.TimeoutError:
                            await asyncio.sleep(0.01); continue
                        except Exception as e_client_input: 
                            print(f"Quart Backend: Error/disconnect receiving from client: {type(e_client_input).__name__}: {e_client_input}")
                            active_processing = False; break
                        if not active_processing: break 
                        await asyncio.sleep(0.01)
                except Exception as e_outer: 
                    print(f"Quart Backend: Error in handle_client_input_and_forward outer loop: {type(e_outer).__name__}: {e_outer}")
                finally:
                    active_processing = False
                    print("Quart Backend: Stopped handling client input.")

            async def receive_from_gemini_and_forward_to_client():
                nonlocal active_processing, current_session_handle # Make current_session_handle nonlocal
                print("Quart Backend: Starting receive_from_gemini_and_forward_to_client task.")
                try:
                    while active_processing: # Add outer loop to keep receiving
                        # Attempt to process one stream of responses from Gemini
                        had_gemini_activity_in_this_iteration = False
                        async for response in session.receive():
                            had_gemini_activity_in_this_iteration = True
                            if not active_processing: break

                            # Handle session resumption updates
                            if response.session_resumption_update:
                                update = response.session_resumption_update
                                if update.resumable and update.new_handle:
                                    current_session_handle = update.new_handle
                                    print(f"Quart Backend: Received session resumption update. New handle: {current_session_handle}")
                                    # The SDK might handle reconnection automatically if the config was set up with a handle.
                                    # For manual re-connection, this handle would be used in a new connect() call.

                            # Process server content (audio, text, errors)
                            if response.data is not None: # This is where audio bytes are usually found in live responses
                                try:
                                    await websocket.send(response.data)
                                except Exception as send_exc:
                                    print(f"Quart Backend: Error sending to client WebSocket: {type(send_exc).__name__}: {send_exc}")
                                    active_processing = False # Critical error sending to client, stop all processing.
                                    break # Exit the session.receive() loop
                            elif response.server_content:
                                if response.server_content.interrupted:
                                    print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                                    print("Quart Backend: Gemini DETECTED AN INTERRUPTION from user audio!")
                                    print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
                                    try:
                                        await websocket.send('{"action": "interrupt_playback"}')
                                        print("Quart Backend: Sent interrupt_playback signal to client.")
                                    except Exception as send_exc:
                                        print(f"Quart Backend: Error sending interrupt_playback signal to client: {type(send_exc).__name__}: {send_exc}")
                                        active_processing = False; break # Critical error if we can't signal client
                                # Check for text parts if response.data is not the primary audio,
                                # or if transcriptions/other text info is present.
                                # Based on documentation, text might be in response.text directly,
                                # or nested in model_turn.parts, or output_transcription.text
                                processed_text_from_server_content = False
                                if response.text is not None: # Direct text attribute
                                    print(f"Quart Backend: Gemini Text (from response.text): {response.text}")
                                    try:
                                        await websocket.send(f"[TEXT_FROM_GEMINI]: {response.text}")
                                    except Exception as send_exc:
                                        print(f"Quart Backend: Error sending text to client WebSocket: {type(send_exc).__name__}: {send_exc}")
                                        active_processing = False; break
                                    processed_text_from_server_content = True
                                
                                if hasattr(response.server_content, 'model_turn') and response.server_content.model_turn and \
                                   hasattr(response.server_content.model_turn, 'parts'):
                                    for part in response.server_content.model_turn.parts:
                                        if part.text:
                                            print(f"Quart Backend: Gemini Text (from model_turn.parts): {part.text}")
                                            try:
                                                await websocket.send(f"[TEXT_FROM_GEMINI]: {part.text}")
                                            except Exception as send_exc:
                                                print(f"Quart Backend: Error sending model_turn text to client WebSocket: {type(send_exc).__name__}: {send_exc}")
                                                active_processing = False; break
                                            processed_text_from_server_content = True
                                    if not active_processing: break # If error occurred in loop
                                
                                if hasattr(response.server_content, 'output_transcription') and \
                                   response.server_content.output_transcription and \
                                   hasattr(response.server_content.output_transcription, 'text'):
                                    transcript = response.server_content.output_transcription.text
                                    if transcript:
                                        print(f"Quart Backend: Gemini Output Transcription: {transcript}")
                                        try:
                                            await websocket.send(f"[TRANSCRIPT_FROM_GEMINI]: {transcript}")
                                        except Exception as send_exc:
                                            print(f"Quart Backend: Error sending transcript to client WebSocket: {type(send_exc).__name__}: {send_exc}")
                                            active_processing = False; break
                                        processed_text_from_server_content = True

                                # Fallback for other potential text or error structures if not caught by specific handlers
                                if not processed_text_from_server_content and not response.data:
                                    print(f"Quart Backend: Received server_content without primary data or known text parts: {response.server_content}")

                            elif hasattr(response, 'error') and response.error:
                                 error_details = response.error
                                 if hasattr(response.error, 'message'): error_details = response.error.message
                                 print(f"Quart Backend: Gemini Error in response: {error_details}")
                                 try:
                                     await websocket.send(f"[ERROR_FROM_GEMINI]: {str(error_details)}")
                                 except Exception as send_exc:
                                     print(f"Quart Backend: Error sending Gemini error to client WebSocket: {type(send_exc).__name__}: {send_exc}")
                                 active_processing = False # Gemini reported an error, stop all processing.
                                 break # Exit the session.receive() loop
                            
                            # The example has `if message.server_content and message.server_content.turn_complete: break`
                            # This implies that the server can signal the end of its turn.
                            # For continuous audio output, this might not apply until the very end.
                            if response.server_content and response.server_content.turn_complete:
                                print("Quart Backend: Gemini server signaled turn_complete.")
                                # For continuous conversation, we don't break the loop here based on turn_complete.
                                # The session remains active until client disconnects or an unrecoverable error.
                        
                        if not active_processing: # Check if any inner break from session.receive() set active_processing to False
                            break # Exit the outer while active_processing loop

                        # If the `async for` loop completed for this turn from Gemini
                        # and active_processing is still true, we should pause briefly before trying to listen again
                        # in the next iteration of the outer `while active_processing` loop.
                        # This prevents a tight loop if session.receive() yields nothing immediately when re-entered.
                        if not had_gemini_activity_in_this_iteration and active_processing: # This check might be redundant if session.receive() blocks
                            await asyncio.sleep(0.1)
                        elif had_gemini_activity_in_this_iteration and active_processing:
                            # If we processed responses, we can immediately try to receive more in the next iteration
                            # of the outer `while active_processing` loop.
                            # No sleep needed here, as `session.receive()` will block until new data or end of stream.
                            pass


                except Exception as e: # This catches errors from the outer while loop or unhandled ones from session.receive()
                    print(f"Quart Backend: Error in Gemini receive processing: {type(e).__name__}: {e}")
                    active_processing = False # An exception here likely means the Gemini session is broken or WebSocket is problematic.
                finally:
                    # This finally block executes when the `while active_processing` loop ends.
                    print("Quart Backend: Stopped receiving from Gemini.")
                    # Ensure active_processing is False so the other task also stops if it hasn't already.
                    active_processing = False


            forward_task = asyncio.create_task(handle_client_input_and_forward(), name="ClientInputForwarder")
            receive_task = asyncio.create_task(receive_from_gemini_and_forward_to_client(), name="GeminiReceiver")
            
            # Run both tasks concurrently. They will run as long as active_processing is True.
            # Exceptions in tasks will be propagated by gather if not handled within the tasks.
            try:
                await asyncio.gather(forward_task, receive_task)
            except Exception as e_gather:
                print(f"Quart Backend: Exception during asyncio.gather: {type(e_gather).__name__}: {e_gather}")
            finally:
                active_processing = False # Ensure loops in tasks will terminate
                # Cancel tasks if they are somehow still running (e.g., stuck in a blocking call not respecting active_processing)
                if not forward_task.done():
                    forward_task.cancel()
                if not receive_task.done():
                    receive_task.cancel()
                # Await cancelled tasks to allow cleanup
                try:
                    await forward_task
                except asyncio.CancelledError:
                    print(f"Quart Backend: Task {forward_task.get_name()} was cancelled during cleanup.")
                except Exception as e_fwd_cleanup:
                     print(f"Quart Backend: Error during forward_task cleanup: {e_fwd_cleanup}")
                try:
                    await receive_task
                except asyncio.CancelledError:
                    print(f"Quart Backend: Task {receive_task.get_name()} was cancelled during cleanup.")
                except Exception as e_rcv_cleanup:
                    print(f"Quart Backend: Error during receive_task cleanup: {e_rcv_cleanup}")

            print("Quart Backend: Gemini interaction tasks finished.")
    except Exception as e_ws_main:
        print(f"Quart Backend: Unhandled error in WebSocket connection: {type(e_ws_main).__name__}: {e_ws_main}")
    finally:
        print("Quart Backend: WebSocket endpoint processing finished.")

# To run this Quart application:
# 1. Install dependencies: pip install quart quart-cors google-generativeai python-dotenv hypercorn
# 2. Set your GEMINI_API_KEY environment variable in a .env file or your system environment.
# 3. Run using Hypercorn:
#    hypercorn main:app --bind 0.0.0.0:8000
#    Or, for development with auto-reload:
#    quart run --host 0.0.0.0 --port 8000 --reload
